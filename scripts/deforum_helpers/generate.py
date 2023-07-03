from PIL import Image
import math
import json
import itertools
import requests
import numexpr
from modules import processing, sd_models
from modules.shared import sd_model, state, cmd_opts, opts, api_aux_keys
from .deforum_controlnet import is_controlnet_enabled, process_with_controlnet
from .prompt import split_weighted_subprompts
from .load_images import load_img, prepare_mask, check_mask_for_errors
from .webui_sd_pipeline import get_webui_sd_pipeline
from .rich import console
from .defaults import get_samplers_list
from .prompt import check_is_number
from PIL import Image
from gradio.processing_utils import encode_pil_to_base64
from enum import Enum
import requests
from io import BytesIO
import base64

def load_mask_latent(mask_input, shape):
    # mask_input (str or PIL Image.Image): Path to the mask image or a PIL Image object
    # shape (list-like len(4)): shape of the image to match, usually latent_image.shape

    if isinstance(mask_input, str):  # mask input is probably a file name
        if mask_input.startswith('http://') or mask_input.startswith('https://'):
            mask_image = Image.open(requests.get(mask_input, stream=True).raw).convert('RGBA')
        else:
            mask_image = Image.open(mask_input).convert('RGBA')
    elif isinstance(mask_input, Image.Image):
        mask_image = mask_input
    else:
        raise Exception("mask_input must be a PIL image or a file name")

    mask_w_h = (shape[-1], shape[-2])
    mask = mask_image.resize(mask_w_h, resample=Image.LANCZOS)
    mask = mask.convert("L")
    return mask

def isJson(myjson):
    try:
        json.loads(myjson)
    except ValueError as e:
        return False
    return True

# Add pairwise implementation here not to upgrade
# the whole python to 3.10 just for one function
def pairwise_repl(iterable):
    a, b = itertools.tee(iterable)
    next(b, None)
    return zip(a, b)

def generate(args, keys, anim_args, loop_args, controlnet_args, root, frame=0, sampler_name=None):
    if state.interrupted:
        return None

    if args.reroll_blank_frames == 'ignore':
        return generate_inner(args, keys, anim_args, loop_args, controlnet_args, root, frame, sampler_name)

    image, caught_vae_exception = generate_with_nans_check(args, keys, anim_args, loop_args, controlnet_args, root, frame, sampler_name)

    if caught_vae_exception or not image.getbbox():
        patience = args.reroll_patience
        print("Blank frame detected! If you don't have the NSFW filter enabled, this may be due to a glitch!")
        if args.reroll_blank_frames == 'reroll':
            while caught_vae_exception or not image.getbbox():
                print("Rerolling with +1 seed...")
                args.seed += 1
                image, caught_vae_exception = generate_with_nans_check(args, keys, anim_args, loop_args, controlnet_args, root, frame, sampler_name)
                patience -= 1
                if patience == 0:
                    print("Rerolling with +1 seed failed for 10 iterations! Try setting webui's precision to 'full' and if it fails, please report this to the devs! Interrupting...")
                    state.interrupted = True
                    state.current_image = image
                    return None
        elif args.reroll_blank_frames == 'interrupt':
            print("Interrupting to save your eyes...")
            state.interrupted = True
            state.current_image = image
            return None
    return image

def generate_with_nans_check(args, keys, anim_args, loop_args, controlnet_args, root, frame=0, sampler_name=None):
    if cmd_opts.disable_nan_check:
        image = generate_inner(args, keys, anim_args, loop_args, controlnet_args, root, frame, sampler_name)
    else:
        try:
            image = generate_inner(args, keys, anim_args, loop_args, controlnet_args, root, frame, sampler_name)
        except Exception as e:
            if "A tensor with all NaNs was produced in VAE." in repr(e):
                print(e)
                return None, True
            else:
                raise e
    return image, False

def generate_inner(args, keys, anim_args, loop_args, controlnet_args, root, frame=0, sampler_name=None):
    # Setup the pipeline
    p = get_webui_sd_pipeline(args, root)
    p.prompt, p.negative_prompt = split_weighted_subprompts(args.prompt, frame, anim_args.max_frames)

    if not args.use_init and args.strength > 0 and args.strength_0_no_init:
        args.strength = 0
    processed = None
    mask_image = None
    init_image = None
    image_init0 = None

    if loop_args.use_looper and anim_args.animation_mode in ['2D', '3D']:
        args.strength = loop_args.imageStrength
        tweeningFrames = loop_args.tweeningFrameSchedule
        blendFactor = .07
        colorCorrectionFactor = loop_args.colorCorrectionFactor
        jsonImages = json.loads(loop_args.imagesToKeyframe)
        # find which image to show
        parsedImages = {}
        frameToChoose = 0
        max_f = anim_args.max_frames - 1

        for key, value in jsonImages.items():
            if check_is_number(key):  # default case 0:(1 + t %5), 30:(5-t%2)
                parsedImages[key] = value
            else:  # math on the left hand side case 0:(1 + t %5), maxKeyframes/2:(5-t%2)
                parsedImages[int(numexpr.evaluate(key))] = value

        framesToImageSwapOn = list(map(int, list(parsedImages.keys())))

        for swappingFrame in framesToImageSwapOn[1:]:
            frameToChoose += (frame >= int(swappingFrame))

        # find which frame to do our swapping on for tweening
        skipFrame = 25
        for fs, fe in pairwise_repl(framesToImageSwapOn):
            if fs <= frame <= fe:
                skipFrame = fe - fs

        if frame % skipFrame <= tweeningFrames:  # number of tweening frames
            blendFactor = loop_args.blendFactorMax - loop_args.blendFactorSlope * math.cos((frame % tweeningFrames) / (tweeningFrames / 2))
        init_image2, _ = load_img(list(jsonImages.values())[frameToChoose],
                                  shape=(args.W, args.H),
                                  use_alpha_as_mask=args.use_alpha_as_mask)
        image_init0 = list(jsonImages.values())[0]

    else:  # they passed in a single init image
        image_init0 = args.init_image

    available_samplers = get_samplers_list()
    if sampler_name is not None:
        if sampler_name in available_samplers.keys():
            p.sampler_name = available_samplers[sampler_name]
        else:
            raise RuntimeError(f"Sampler name '{sampler_name}' is invalid. Please check the available sampler list in the 'Run' tab")

    if args.checkpoint is not None:
        info = sd_models.get_closet_checkpoint_match(args.checkpoint)
        if info is None:
            raise RuntimeError(f"Unknown checkpoint: {args.checkpoint}")
        sd_models.reload_model_weights(info=info)

    if root.init_sample is not None:
        # TODO: cleanup init_sample remains later
        img = root.init_sample
        init_image = img
        image_init0 = img
        if loop_args.use_looper and isJson(loop_args.imagesToKeyframe) and anim_args.animation_mode in ['2D', '3D']:
            init_image = Image.blend(init_image, init_image2, blendFactor)
            correction_colors = Image.blend(init_image, init_image2, colorCorrectionFactor)
            p.color_corrections = [processing.setup_color_correction(correction_colors)]

    # this is the first pass
    elif (loop_args.use_looper and anim_args.animation_mode in ['2D', '3D']) or (args.use_init and ((args.init_image != None and args.init_image != ''))):
        init_image, mask_image = load_img(image_init0,  # initial init image
                                          shape=(args.W, args.H),
                                          use_alpha_as_mask=args.use_alpha_as_mask)

    else:

        if anim_args.animation_mode != 'Interpolation':
            print(f"Not using an init image (doing pure txt2img)")
        p_txt = processing.StableDiffusionProcessingTxt2Img(
            sd_model=sd_model,
            outpath_samples=root.tmp_deforum_run_duplicated_folder,
            outpath_grids=root.tmp_deforum_run_duplicated_folder,
            prompt=p.prompt,
            styles=p.styles,
            negative_prompt=p.negative_prompt,
            seed=p.seed,
            subseed=p.subseed,
            subseed_strength=p.subseed_strength,
            seed_resize_from_h=p.seed_resize_from_h,
            seed_resize_from_w=p.seed_resize_from_w,
            sampler_name=p.sampler_name,
            batch_size=p.batch_size,
            n_iter=p.n_iter,
            steps=p.steps,
            cfg_scale=p.cfg_scale,
            width=p.width,
            height=p.height,
            restore_faces=p.restore_faces,
            tiling=p.tiling,
            enable_hr=False,
            denoising_strength=0,
        )

        print_combined_table(args, anim_args, p_txt, keys, frame)  # print dynamic table to cli

        if cmd_opts.just_ui:
            import uuid
            simple_txt2img = {
                "firstphase_width": 0,
                "firstphase_height": 0,
                "prompt": p.prompt,
                "styles": p.styles,
                "seed": p.seed,
                "subseed": p.subseed,
                "subseed_strength": p.subseed_strength,
                "seed_resize_from_h": p.seed_resize_from_h,
                "seed_resize_from_w": p.seed_resize_from_w,
                "batch_size": p.batch_size,
                "n_iter": p.n_iter,
                "steps": p.steps,
                "cfg_scale": p.cfg_scale,
                "width": p.width,
                "height": p.height,
                "restore_faces": p.restore_faces,
                "enable_hr": False,
                "denoising_strength": 0,
                "tiling": p.tiling,
                "negative_prompt": p.negative_prompt,
                "sampler_index": p.sampler_name,
                "alwayson_scripts":{
                    "sd_model_checkpoint": p.sd_model.sd_checkpoint_info.model_name, 
                    "id_task": str(uuid.uuid1()), 
                    "uid": cmd_opts.uid, 
                    "sd_vae": opts.sd_vae
                },
            }
            override_settings = {}
            setting_dict = opts.__dict__['data']
            for key in api_aux_keys:
                override_settings[key] = setting_dict.get(key)
            simple_txt2img["alwayson_scripts"]["override_settings"] = override_settings

        if is_controlnet_enabled(controlnet_args):
            if cmd_opts.just_ui:
                simple_txt2img['alwayson_scripts']['controlnet'] = {'args':[]}
                cn_args = process_with_controlnet(p_txt, args, anim_args, controlnet_args, root, is_img2img=False, frame_idx=frame)
                for cn_arg in cn_args:
                    for cn_key, cn_val in cn_arg.items():
                        if isinstance(cn_val, Enum):
                            cn_val = cn_val.value
                        elif (cn_key == 'image') and (isinstance(cn_val, dict)):
                            if cn_val.get('image', None) is not None:
                                cn_val['image'] = encode_pil_to_base64(Image.fromarray(cn_val['image']))
                            if cn_val.get('mask', None) is not None:
                                cn_val['mask'] = encode_pil_to_base64(Image.fromarray(cn_val['mask']))
                        cn_arg[cn_key] = cn_val
                    simple_txt2img['alwayson_scripts']['controlnet']['args'].append(cn_args)
            else:
                process_with_controlnet(p_txt, args, anim_args, controlnet_args, root, is_img2img=False, frame_idx=frame)
        if cmd_opts.just_ui:
            url_txt2img = '/'.join([cmd_opts.server_path, 'sdapi/v1/txt2img'])
            data = requests.post(url_txt2img, json=simple_txt2img, headers={'x-eas-uid': cmd_opts.uid})
            if data.status_code != 200:
                processed = processing.Processed(p_txt,[],comments=data.text)
            else:
                data_text = json.loads(data.text)
                imgs = data_text['images']
                info = json.loads(data_text['info'])
                if root.initial_info is None:
                    root.initial_info = info['infotexts']
                for img_i, img in enumerate(imgs):
                    im = Image.open(BytesIO(base64.b64decode(img)))
                    if root.first_frame is None:
                        root.first_frame = im
                    return im
        processed = processing.process_images(p_txt)

    if processed is None:
        # Mask functions
        if args.use_mask:
            mask_image = args.mask_image
            mask = prepare_mask(args.mask_file if mask_image is None else mask_image,
                                (args.W, args.H),
                                args.mask_contrast_adjust,
                                args.mask_brightness_adjust)
            p.inpainting_mask_invert = args.invert_mask
            p.inpainting_fill = args.fill
            p.inpaint_full_res = args.full_res_mask
            p.inpaint_full_res_padding = args.full_res_mask_padding
            # prevent loaded mask from throwing errors in Image operations if completely black and crop and resize in webui pipeline
            # doing this after contrast and brightness adjustments to ensure that mask is not passed as black or blank
            mask = check_mask_for_errors(mask, args.invert_mask)
            root.noise_mask = mask
        else:
            mask = None

        assert not ((mask is not None and args.use_mask and args.overlay_mask) and (
                root.init_sample is None and init_image is None)), "Need an init image when use_mask == True and overlay_mask == True"

        p.init_images = [init_image]
        p.image_mask = mask
        p.image_cfg_scale = args.pix2pix_img_cfg_scale
        if cmd_opts.just_ui:
            import uuid
            simple_img2img = {
                "firstphase_width": 0,
                "firstphase_height": 0,
                "prompt": p.prompt,
                "styles": p.styles,
                "seed": p.seed,
                "subseed": p.subseed,
                "subseed_strength": p.subseed_strength,
                "seed_resize_from_h": p.seed_resize_from_h,
                "seed_resize_from_w": p.seed_resize_from_w,
                "batch_size": p.batch_size,
                "n_iter": p.n_iter,
                "steps": p.steps,
                "cfg_scale": p.cfg_scale,
                "width": p.width,
                "height": p.height,
                "restore_faces": p.restore_faces,
                "enable_hr": False,
                "denoising_strength": 0,
                "tiling": p.tiling,
                "negative_prompt": p.negative_prompt,
                "sampler_index": p.sampler_name,
                "init_images": [encode_pil_to_base64(init_image)] if init_image is not None else None,
                "mask": encode_pil_to_base64(mask) if mask is not None else None,
                "inpainting_fill": p.inpainting_fill,
                "inpaint_full_res": p.inpaint_full_res,
                "inpaint_full_res_padding": p.inpaint_full_res_padding,
                "inpainting_mask_invert": p.inpainting_mask_invert,
                "denoising_strength": p.denoising_strength,
                "image_cfg_scale": p.image_cfg_scale,
                "alwayson_scripts":{
                    "sd_model_checkpoint": p.sd_model.sd_checkpoint_info.model_name, 
                    "id_task": str(uuid.uuid1()), 
                    "uid": cmd_opts.uid, 
                    "sd_vae": opts.sd_vae
                },
            }
            override_settings = {}
            setting_dict = opts.__dict__['data']
            for key in api_aux_keys:
                override_settings[key] = setting_dict.get(key)
            simple_img2img["alwayson_scripts"]["override_settings"] = override_settings


        print_combined_table(args, anim_args, p, keys, frame)  # print dynamic table to cli

        if is_controlnet_enabled(controlnet_args):
            if cmd_opts.just_ui:
                simple_img2img['alwayson_scripts']['controlnet'] = {'args':[]}
                cn_args = process_with_controlnet(p_txt, args, anim_args, controlnet_args, root, is_img2img=False, frame_idx=frame)
                for cn_arg in cn_args:
                    for cn_key, cn_val in cn_arg.items():
                        if isinstance(cn_val, Enum):
                            cn_val = cn_val.value
                        elif (cn_key == 'image') and (isinstance(cn_val, dict)):
                            if cn_val.get('image', None) is not None:
                                cn_val['image'] = encode_pil_to_base64(Image.fromarray(cn_val['image']))
                            if cn_val.get('mask', None) is not None:
                                cn_val['mask'] = encode_pil_to_base64(Image.fromarray(cn_val['mask']))
                        cn_arg[cn_key] = cn_val
                    simple_img2img['alwayson_scripts']['controlnet']['args'].append(cn_args)
            else:
                process_with_controlnet(p, args, anim_args, controlnet_args, root, is_img2img=True, frame_idx=frame)
        if cmd_opts.just_ui:
            url_img2img = '/'.join([cmd_opts.server_path, 'sdapi/v1/img2img'])
            data = requests.post(url_img2img, json=simple_img2img, headers={'x-eas-uid': cmd_opts.uid})
            if data.status_code != 200:
                processed = processing.Processed(p,[],comments=data.text)
            else:
                data_text = json.loads(data.text)
                imgs = data_text['images']
                info = json.loads(data_text['info'])
                if root.initial_info is None:
                    root.initial_info = info['infotexts']
                for img_i, img in enumerate(imgs):
                    im = Image.open(BytesIO(base64.b64decode(img)))
                    if root.first_frame is None:
                        root.first_frame = im
                    return im

        processed = processing.process_images(p)

    if root.initial_info is None:
        root.initial_info = processed.info

    if root.first_frame is None:
        root.first_frame = processed.images[0]

    results = processed.images[0]

    return results

def print_combined_table(args, anim_args, p, keys, frame_idx):
    from rich.table import Table
    from rich import box

    table = Table(padding=0, box=box.ROUNDED)

    field_names1 = ["Steps", "CFG"]
    if anim_args.animation_mode != 'Interpolation':
        field_names1.append("Denoise")
    field_names1 += ["Subseed", "Subs. str"] * (anim_args.enable_subseed_scheduling)
    field_names1 += ["Sampler"] * anim_args.enable_sampler_scheduling
    field_names1 += ["Checkpoint"] * anim_args.enable_checkpoint_scheduling

    for field_name in field_names1:
        table.add_column(field_name, justify="center")

    rows1 = [str(p.steps), str(p.cfg_scale)]
    if anim_args.animation_mode != 'Interpolation':
        rows1.append(f"{p.denoising_strength:.5g}" if p.denoising_strength is not None else "None")

    rows1 += [str(p.subseed), f"{p.subseed_strength:.5g}"] * anim_args.enable_subseed_scheduling
    rows1 += [p.sampler_name] * anim_args.enable_sampler_scheduling
    rows1 += [str(args.checkpoint)] * anim_args.enable_checkpoint_scheduling

    rows2 = []
    if anim_args.animation_mode not in ['Video Input', 'Interpolation']:
        if anim_args.animation_mode == '2D':
            field_names2 = ["Angle", "Zoom"]
        else:
            field_names2 = []
        field_names2 += ["Tr X", "Tr Y"]
        if anim_args.animation_mode == '3D':
            field_names2 += ["Tr Z", "Ro X", "Ro Y", "Ro Z"]
            if anim_args.aspect_ratio_schedule.replace(" ", "") != '0:(1)':
                field_names2 += ["Asp. Ratio"]
        if anim_args.enable_perspective_flip:
            field_names2 += ["Pf T", "Pf P", "Pf G", "Pf F"]

        for field_name in field_names2:
            table.add_column(field_name, justify="center")

        if anim_args.animation_mode == '2D':
            rows2 += [f"{keys.angle_series[frame_idx]:.5g}", f"{keys.zoom_series[frame_idx]:.5g}"]
        rows2 += [f"{keys.translation_x_series[frame_idx]:.5g}", f"{keys.translation_y_series[frame_idx]:.5g}"]

        if anim_args.animation_mode == '3D':
            rows2 += [f"{keys.translation_z_series[frame_idx]:.5g}", f"{keys.rotation_3d_x_series[frame_idx]:.5g}",
                      f"{keys.rotation_3d_y_series[frame_idx]:.5g}", f"{keys.rotation_3d_z_series[frame_idx]:.5g}"]
            if anim_args.aspect_ratio_schedule.replace(" ", "") != '0:(1)':
                rows2 += [f"{keys.aspect_ratio_series[frame_idx]:.5g}"]
        if anim_args.enable_perspective_flip:
            rows2 += [f"{keys.perspective_flip_theta_series[frame_idx]:.5g}", f"{keys.perspective_flip_phi_series[frame_idx]:.5g}",
                      f"{keys.perspective_flip_gamma_series[frame_idx]:.5g}", f"{keys.perspective_flip_fv_series[frame_idx]:.5g}"]

    table.add_row(*rows1, *rows2)
    console.print(table)
