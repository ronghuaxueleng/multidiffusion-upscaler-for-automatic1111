# ------------------------------------------------------------------------
#
#   Ultimate VAE Tile Optimization
#
#   Introducing a revolutionary new optimization designed to make
#   the VAE work with giant images on limited VRAM!
#   Say goodbye to the frustration of OOM and hello to seamless output!
#
# ------------------------------------------------------------------------
#
#   This script is a wild hack that splits the image into tiles,
#   encodes each tile separately, and merges the result back together.
#
#   Advantages:
#   - The VAE can now work with giant images on limited VRAM
#       (~10 GB for 8K images!)
#   - The merged output is completely seamless without any post-processing.
#
#   Drawbacks:
#   - Giant RAM needed. To store the intermediate results for a 4096x4096
#       images, you need 32 GB RAM it consumes ~20GB); for 8192x8192
#       you need 128 GB RAM machine (it consumes ~100 GB)
#   - NaNs always appear in for 8k images when you use fp16 (half) VAE
#       You must use --no-half-vae to disable half VAE for that giant image.
#   - Slow speed. With default tile size, it takes around 50/200 seconds
#       to encode/decode a 4096x4096 image; and 200/900 seconds to encode/decode
#       a 8192x8192 image. (The speed is limited by both the GPU and the CPU.)
#   - The gradient calculation is not compatible with this hack. It
#       will break any backward() or torch.autograd.grad() that passes VAE.
#       (But you can still use the VAE to generate training data.)
#
#   How it works:
#   1) The image is split into tiles.
#       - To ensure perfect results, each tile is padded with 32 pixels
#           on each side.
#       - Then the conv2d/silu/upsample/downsample can produce identical 
#           results to the original image without splitting.
#   2) The original forward is decomposed into a task queue and a task worker.
#       - The task queue is a list of functions that will be executed in order.
#       - The task worker is a loop that executes the tasks in the queue.
#   3) The task queue is executed for each tile.
#       - Current tile is sent to GPU.
#       - local operations are directly executed.
#       - Group norm calculation is temporarily suspended until the mean
#           and var of all tiles are calculated.
#       - The residual is pre-calculated and stored and addded back later.
#       - When need to go to the next tile, the current tile is send to cpu.
#   4) After all tiles are processed, tiles are merged on cpu and return.
#
#   Enjoy!
#
#   @author: LI YI @ Nanyang Technological University - Singapore
#   @date: 2023-03-02
#   @license: MIT License
#
#   Please give me a star if you like this project!
#
# -------------------------------------------------------------------------

import gc
from time import time
import math
from tqdm import tqdm

import torch
import torch.nn.functional as F
import gradio as gr

import modules.scripts as scripts
import modules.devices as devices
from modules.shared import state


def get_recommend_encoder_tile_size():
    if torch.cuda.is_available():
        total_memory = torch.cuda.get_device_properties(
            devices.device).total_memory // 2**20
        if total_memory > 16*1000:
            ENCODER_TILE_SIZE = 3072
        elif total_memory > 12*1000:
            ENCODER_TILE_SIZE = 2048
        elif total_memory > 8*1000:
            ENCODER_TILE_SIZE = 1536
        else:
            ENCODER_TILE_SIZE = 960
    else:
        ENCODER_TILE_SIZE = 512
    return ENCODER_TILE_SIZE


def get_recommend_decoder_tile_size():
    if torch.cuda.is_available():
        total_memory = torch.cuda.get_device_properties(
            devices.device).total_memory // 2**20
        if total_memory > 30*1000:
            DECODER_TILE_SIZE = 256
        elif total_memory > 16*1000:
            DECODER_TILE_SIZE = 192
        elif total_memory > 12*1000:
            DECODER_TILE_SIZE = 128
        elif total_memory > 8*1000:
            DECODER_TILE_SIZE = 96
        else:
            DECODER_TILE_SIZE = 64
    else:
        DECODER_TILE_SIZE = 64
    return DECODER_TILE_SIZE


if 'global const':
    DEFAULT_ENABLED = True
    DEFAULT_ENCODER_TILE_SIZE = get_recommend_encoder_tile_size()
    DEFAULT_DECODER_TILE_SIZE = get_recommend_decoder_tile_size()


# inplace version of silu
def inplace_nonlinearity(x):
    # Test: fix for Nans
    return F.silu(x, inplace=True)


def resblock2task(queue, block):
    """
    Turn a ResNetBlock into a sequence of tasks and append to the task queue

    @param queue: the target task queue
    @param block: ResNetBlock

    """
    if block.in_channels != block.out_channels:
        if block.use_conv_shortcut:
            queue.append(('store_res', block.conv_shortcut))
        else:
            queue.append(('store_res', block.nin_shortcut))
    else:
        queue.append(('store_res', lambda x: x))
    queue.append(('pre_norm', block.norm1))
    queue.append(('silu', inplace_nonlinearity))
    queue.append(('conv1', block.conv1))
    queue.append(('temb', lambda h, temb: h +
                 block.temb_proj(inplace_nonlinearity(temb))[:, :, None, None]))
    queue.append(('pre_norm', block.norm2))
    queue.append(('silu', inplace_nonlinearity))
    queue.append(('conv2', block.conv2))
    queue.append(['add_res', None])


def build_sampling(task_queue, net, is_decoder):
    """
    Build the sampling part of a task queue
    @param task_queue: the target task queue
    @param net: the network
    @param is_decoder: currently building decoder or encoder
    """
    if is_decoder:
        resblock2task(task_queue, net.mid.block_1)
        resblock2task(task_queue, net.mid.block_2)
        resolution_iter = reversed(range(net.num_resolutions))
        block_ids = net.num_res_blocks + 1
        condition = 0
        module = net.up
        func_name = 'upsample'
    else:
        resolution_iter = range(net.num_resolutions)
        block_ids = net.num_res_blocks
        condition = net.num_resolutions - 1
        module = net.down
        func_name = 'downsample'

    for i_level in resolution_iter:
        for i_block in range(block_ids):
            resblock2task(task_queue, module[i_level].block[i_block])
        if i_level != condition:
            task_queue.append((func_name, getattr(module[i_level], func_name)))

    if not is_decoder:
        resblock2task(task_queue, net.mid.block_1)
        resblock2task(task_queue, net.mid.block_2)


def build_task_queue(net, is_decoder):
    """
    Build a single task queue for the encoder or decoder
    @param net: the VAE decoder or encoder network
    @param is_decoder: currently building decoder or encoder
    @return: the task queue
    """
    task_queue = []
    task_queue.append(('conv_in', net.conv_in))

    # construct the sampling part of the task queue
    # because encoder and decoder share the same architecture, we extract the sampling part
    build_sampling(task_queue, net, is_decoder)

    if not is_decoder or not net.give_pre_end:
        task_queue.append(('pre_norm', net.norm_out))
        task_queue.append(('silu', inplace_nonlinearity))
        task_queue.append(('conv_out', net.conv_out))
        if is_decoder and net.tanh_out:
            task_queue.append(('tanh', torch.tanh))

    return task_queue


def get_var_mean(input, num_groups, eps=1e-6):
    """
    Get mean and var for group norm
    """
    b, c = input.size(0), input.size(1)
    channel_in_group = int(c/num_groups)
    input_reshaped = input.contiguous().view(
        1, int(b * num_groups), channel_in_group, *input.size()[2:])
    var, mean = torch.var_mean(
        input_reshaped, dim=[0, 2, 3, 4], unbiased=False)
    return var, mean


def custom_group_norm(input, num_groups, mean, var, weight=None, bias=None, eps=1e-6):
    """
    Custom group norm with fixed mean and var

    @param input: input tensor
    @param num_groups: number of groups. by default, num_groups = 32
    @param mean: mean, must be pre-calculated by get_var_mean
    @param var: var, must be pre-calculated by get_var_mean
    @param weight: weight, should be fetched from the original group norm
    @param bias: bias, should be fetched from the original group norm
    @param eps: epsilon, by default, eps = 1e-6 to match the original group norm

    @return: normalized tensor
    """
    b, c = input.size(0), input.size(1)
    channel_in_group = int(c/num_groups)
    input_reshaped = input.contiguous().view(
        1, int(b * num_groups), channel_in_group, *input.size()[2:])

    out = F.batch_norm(input_reshaped, mean, var, weight=None, bias=None,
                       training=False, momentum=0, eps=eps)

    out = out.view(b, c, *input.size()[2:])

    # post affine transform
    if weight is not None:
        out *= weight.view(1, -1, 1, 1)
    if bias is not None:
        out += bias.view(1, -1, 1, 1)
    return out


def crop_valid_region(x, input_bbox, target_bbox, scale):
    """
    Crop the valid region from the tile
    @param x: input tile
    @param input_bbox: original input bounding box
    @param target_bbox: output bounding box
    @param scale: scale factor
    @return: cropped tile
    """
    padded_bbox = [math.ceil(i*scale) for i in input_bbox]
    margin = [target_bbox[i] - padded_bbox[i] for i in range(4)]
    return x[:, :, margin[2]:x.size(2)+margin[3], margin[0]:x.size(3)+margin[1]]

# ↓↓↓ https://github.com/Kahsolt/stable-diffusion-webui-vae-tile-infer ↓↓↓


def perfcount(fn):
    def wrapper(*args, **kwargs):
        ts = time()

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats(devices.device)
        devices.torch_gc()
        gc.collect()

        ret = fn(*args, **kwargs)

        devices.torch_gc()
        gc.collect()
        if torch.cuda.is_available():
            vram = torch.cuda.max_memory_allocated(devices.device) / 2**20
            torch.cuda.reset_peak_memory_stats(devices.device)
            print(f'[Tiled VAE]: Done in {time() - ts:.3f}s, max VRAM alloc {vram:.3f} MB')
        else:
            print(f'[Tiled VAE]: Done in {time() - ts:.3f}s')

        return ret
    return wrapper

# copy end :)


class GroupNormParam:
    def __init__(self):
        self.var_list = []
        self.mean_list = []
        self.pixel_list = []
        self.weight = None
        self.bias = None
    
    def add_tile(self, tile, layer):
        var, mean = get_var_mean(tile, 32)
        # For giant images, the variance can be larger than max float16
        # In this case we create a copy to float32
        if var.dtype == torch.float16 and var.isinf().any():
            fp32_tile = tile.float()
            var, mean = get_var_mean(fp32_tile, 32)
        # ============= DEBUG: test for infinite =============
        # if torch.isinf(var).any():
        #    print('var: ', var)
        # ====================================================
        self.var_list.append(var)
        self.mean_list.append(mean)
        self.pixel_list.append(
            tile.shape[2]*tile.shape[3])
        if hasattr(layer, 'weight'):
            self.weight = layer.weight
            self.bias = layer.bias
        else:
            self.weight = None
            self.bias = None

    def summary(self):
        """
        summarize the mean and var and return a function
        that apply group norm on each tile
        """
        if len(self.var_list) == 0:
            return None
        var = torch.vstack(self.var_list)
        mean = torch.vstack(self.mean_list)
        max_value = max(self.pixel_list)
        pixels = torch.tensor(
            self.pixel_list, dtype=torch.float32, device=devices.device) / max_value
        sum_pixels = torch.sum(pixels)
        pixels = pixels.unsqueeze(
            1) / sum_pixels
        var = torch.sum(
            var * pixels, dim=0)
        mean = torch.sum(
            mean * pixels, dim=0)
        return lambda x:  custom_group_norm(x, 32, mean, var, self.weight, self.bias)
    


class VAEHook:

    def __init__(self, net, tile_size, is_decoder):
        self.net = net                  # encoder | decoder
        self.tile_size = tile_size
        self.is_decoder = is_decoder
        self.pad = 11 if is_decoder else 32

    def __call__(self, x):
        B, C, H, W = x.shape
        if max(H, W) <= self.pad * 2 + self.tile_size:
            print("[Tiled VAE]: the input size is tiny and unnecessary to tile.")
            return self.net.original_forward(x)
        else:
            return self.vae_tile_forward(x)
    
    def get_best_tile_size(self, lowerbound, upperbound):
        """
        Get the best tile size for GPU memory
        """
        divider = 32
        while divider >= 2:
            remainer = lowerbound % divider
            if remainer == 0:
                return lowerbound
            candidate = lowerbound - remainer + divider
            if candidate <= upperbound:
                return candidate
            divider //= 2
        return lowerbound
        
    def split_tiles(self, h, w):
        """
        Tool function to split the image into tiles
        @param h: height of the image
        @param w: width of the image
        @return: tile_input_bboxes, tile_output_bboxes
        """
        tile_input_bboxes, tile_output_bboxes = [], []
        tile_size = self.tile_size
        pad = self.pad
        num_height_tiles = math.ceil((h - 2 * pad) / tile_size)
        num_width_tiles = math.ceil((w - 2 * pad) / tile_size)
        # If any of the numbers are 0, we let it be 1
        # This is to deal with long and thin images
        num_height_tiles = max(num_height_tiles, 1)
        num_width_tiles = max(num_width_tiles, 1)

        # Suggestions from https://github.com/Kahsolt: auto shrink the tile size
        real_tile_height = math.ceil((h - 2 * pad) / num_height_tiles)
        real_tile_width = math.ceil((w - 2 * pad) / num_width_tiles)
        real_tile_height = self.get_best_tile_size(real_tile_height, tile_size)
        real_tile_width = self.get_best_tile_size(real_tile_width, tile_size)

        print(f'[Tiled VAE]: split to {num_height_tiles}x{num_width_tiles} = {num_height_tiles*num_width_tiles} tiles. ' + \
              f'Optimal tile size {real_tile_width}x{real_tile_height}, original tile size {tile_size}x{tile_size}')

        for i in range(num_height_tiles):
            for j in range(num_width_tiles):
                # bbox: [x1, x2, y1, y2]
                # the padding is is unnessary for image borders. So we directly start from (32, 32)
                input_bbox = [
                    pad + j * real_tile_width,
                    min(pad + (j + 1) * real_tile_width, w),
                    pad + i * real_tile_height,
                    min(pad + (i + 1) * real_tile_height, h),
                ]

                # if the output bbox is close to the image boundary, we extend it to the image boundary
                output_bbox = [
                    input_bbox[0] if input_bbox[0] > pad else 0,
                    input_bbox[1] if input_bbox[1] < w - pad else w,
                    input_bbox[2] if input_bbox[2] > pad else 0,
                    input_bbox[3] if input_bbox[3] < h - pad else h,
                ]

                # scale to get the final output bbox
                scale_factor = 8 if self.is_decoder else 1/8
                output_bbox = [math.ceil(x * scale_factor)
                               for x in output_bbox]
                tile_output_bboxes.append(output_bbox)

                # indistinguishable expand the input bbox by pad pixels
                tile_input_bboxes.append([
                    max(0, input_bbox[0] - pad),
                    min(w, input_bbox[1] + pad),
                    max(0, input_bbox[2] - pad),
                    min(h, input_bbox[3] + pad),
                ])

        return tile_input_bboxes, tile_output_bboxes

    @perfcount
    @torch.inference_mode()
    def vae_tile_forward(self, z):
        """
        Decode a latent vector z into an image in a tiled manner.
        @param z: latent vector
        @return: image
        """
        net = self.net
        tile_size = self.tile_size
        is_decoder = self.is_decoder

        N, height, width = z.shape[0], z.shape[2], z.shape[3]
        net.last_z_shape = z.shape
        device = z.device

        # Split the input into tiles and build a task queue for each tile
        print(f'[Tiled VAE]: input_size: {z.shape}, tile_size: {tile_size}, padding: {self.pad}')

        in_bboxes, out_bboxes = self.split_tiles(height, width)

        # Prepare tiles by split the input latents
        tiles = []
        for input_bbox in in_bboxes:
            tile = z[:, :, input_bbox[2]:input_bbox[3],
                     input_bbox[0]:input_bbox[1]].cpu()
            tiles.append(tile)

        num_tiles = len(tiles)
        num_completed = 0

        # Free memory of input latent tensor
        del z
        result = None

        # Build task queues
        task_queues = [build_task_queue(net, is_decoder).copy()
                       for _ in range(num_tiles)]

        # Task queue execution
        desc = f"[Tiled VAE]: Executing {'Decoder' if is_decoder else 'Encoder'} Task Queue: "
        pbar = tqdm(total=num_tiles * len(task_queues[0]), desc=desc)

        # execute the task back and forth when switch tiles so that we always
        # keep one tile on the GPU to reduce unnecessary data transfer
        forward = True
        while True:
            group_norm_param = GroupNormParam()
            for i in range(num_tiles) if forward else reversed(range(num_tiles)):
                if state.interrupted: return

                tile = tiles[i].to(device)
                input_bbox = in_bboxes[i]
                task_queue = task_queues[i]

                while len(task_queue) > 0:
                    if state.interrupted:
                        return
                    # DEBUG: current task
                    # print('Running task: ', task_queue[0][0], ' on tile ', i, '/', num_tiles, ' with shape ', tile.shape)
                    task = task_queue.pop(0)
                    if task[0] == 'pre_norm':
                        group_norm_param.add_tile(tile, task[1])
                        break
                    elif task[0] == 'store_res':
                        task_id = 0
                        while task_queue[task_id][0] != 'add_res':
                            task_id += 1
                        task_queue[task_id][1] = task[1](tile).cpu()
                    elif task[0] == 'add_res':
                        tile = tile + task[1].to(device)
                    elif task[0] == 'temb':
                        pass
                    else:
                        tile = task[1](tile)
                    pbar.update(1)

                # check for NaNs in the tile.
                # If there are NaNs, we abort the process to save user's time
                try:
                    devices.test_for_nans(tile, "vae")
                except:
                    print("Detected NaNs in the VAE output. Please try --no-half-vae")
                    raise

                if len(task_queue) == 0:
                    del tiles[i]
                    num_completed += 1
                    if result is None:
                        scale_factor = 8 if self.is_decoder else 1/8
                        result = torch.zeros((N, tile.shape[1], math.ceil(height * scale_factor), math.ceil(width * scale_factor)), device=device)
                    result[:,:,out_bboxes[i][2]:out_bboxes[i][3],out_bboxes[i][0]:out_bboxes[i][1]] = crop_valid_region(tile, in_bboxes[i], 
                                                     out_bboxes[i], 8 if is_decoder else 1/8)
                    del tile
                elif i == num_tiles - 1 and forward:
                    forward = False
                    tiles[i] = tile
                elif i == 0 and not forward:
                    forward = True
                    tiles[i] = tile
                else:
                    tiles[i] = tile.cpu()
                    del tile

            if num_completed == num_tiles:
                break

            # insert the group norm task to the head of each task queue
            group_norm_func = group_norm_param.summary()
            if group_norm_func is not None:
                for i in range(num_tiles):
                    task_queue = task_queues[i]
                    task_queue.insert(0, ('apply_norm', group_norm_func))

        # Done!
        pbar.close()
        return result


class Script(scripts.Script):

    def title(self):
        return "Tiled VAE"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    def ui(self, is_img2img):
        with gr.Accordion('Tiled VAE', open=False):
            with gr.Row():
                enabled = gr.Checkbox(
                    label='Enable', value=lambda: DEFAULT_ENABLED)
                reset = gr.Button(value="Reset Tile Size")

            info = gr.HTML(
                '<p style="margin-bottom:0.8em">Please use smaller tile size when see CUDA error: out of memory.</p>')

            with gr.Row():
                encoder_tile_size = gr.Slider(
                    label='Encoder Tile Size', minimum=256, maximum=4096, step=16, value=lambda: DEFAULT_ENCODER_TILE_SIZE)
                decoder_tile_size = gr.Slider(
                    label='Decoder Tile Size', minimum=48,  maximum=512,  step=16, value=lambda: DEFAULT_DECODER_TILE_SIZE)

                reset.click(fn=lambda: [DEFAULT_ENCODER_TILE_SIZE, DEFAULT_DECODER_TILE_SIZE], outputs=[
                            encoder_tile_size, decoder_tile_size])

        return enabled, encoder_tile_size, decoder_tile_size

    def process(self, p, enabled, encoder_tile_size, decoder_tile_size):
        vae = p.sd_model.first_stage_model
        if vae.device == torch.device('cpu'):
            print("[Tiled VAE] Tiled VAE is not needed as your VAE is in CPU RAM. ")
            print("[Tiled VAE] If you want to enable, please DON'T USE --lowvram or --medvram on webui startup.")
            return

        # for shorthand
        encoder = vae.encoder
        decoder = vae.decoder

        # save original forward (only once)
        if not hasattr(encoder, 'original_forward'): setattr(encoder, 'original_forward', encoder.forward)
        if not hasattr(decoder, 'original_forward'): setattr(decoder, 'original_forward', decoder.forward)

        # undo hijack if disabled
        if not enabled:
            encoder.forward = encoder.original_forward
            decoder.forward = decoder.original_forward
            return

        # do hijack
        encoder.forward = VAEHook(encoder, encoder_tile_size, is_decoder=False)
        decoder.forward = VAEHook(decoder, decoder_tile_size, is_decoder=True)