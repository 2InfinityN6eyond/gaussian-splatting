#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import torch
from random import randint
from utils.loss_utils import l1_loss, ssim
from gaussian_renderer import render, network_gui
import sys
from scene import Scene, GaussianModel
from utils.general_utils import safe_state
import uuid
from tqdm import tqdm
from utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams
try: 
    from torch.utils.tensorboard import SummaryWriter
    TENSORBOARD_FOUND = True
except ImportError:
    TENSORBOARD_FOUND = False

def training(
    model_args              : ModelParams,
    optim_args              : OptimizationParams,
    pipe_args               : PipelineParams,
    testing_iterations,     # List[int]
    saving_iterations,      # List[int]
    checkpoint_iterations,  # List[int]
    start_checkpoint        : str,
    debug_from              : int
):
    """
    args:
        model_args              : ModelParams
        optim_args              : OptimizationParams
        pipe_args               : PipelineParams
        testing_iterations      : int
        saving_iterations       : int 
        checkpoint_iterations   : int
        start_checkpoint        : int
        debug_from              : int
    """
    first_iter = 0
    tb_writer = prepare_output_and_logger(model_args)
    gaussians = GaussianModel(model_args.sh_degree)
    scene = Scene(model_args, gaussians)
    gaussians.training_setup(optim_args)
    if start_checkpoint:
        # model_params : saved return value of GaussianModel.capture()
        # first_iter : last saved iteration idx
        print("Loading checkpoint from {}".format(start_checkpoint))
        (model_params, first_iter) = torch.load(start_checkpoint)
        gaussians.restore(model_params, optim_args)

    bg_color = [1, 1, 1] if model_args.white_background else [0, 0, 0]
    background = torch.tensor(bg_color, dtype=torch.float32, device="cuda")

    iter_start = torch.cuda.Event(enable_timing = True)
    iter_end = torch.cuda.Event(enable_timing = True)

    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, optim_args.iterations), desc="Training progress")
    first_iter += 1
    for iter_idx in range(first_iter, optim_args.iterations + 1):        
        if network_gui.conn == None:
            network_gui.try_connect()
        # communicate with GUI
        while network_gui.conn != None:
            try:
                net_image_bytes = None
                (
                    custom_cam,                     # virtual camera set on GUI
                    do_training,                    # 
                    pipe_args.convert_SHs_python,   #
                    pipe_args.compute_cov3D_python, #
                    keep_alive,                     #
                    scaling_modifer                 #
                ) = network_gui.receive()
                if custom_cam != None:
                    with torch.no_grad() :
                        net_image = render( # render image for visualization.
                            custom_cam, gaussians, pipe_args,
                            background, scaling_modifer
                        )["render"]
                    net_image_bytes = memoryview(
                        (
                            torch.clamp(net_image, min=0, max=1.0) * 255
                        ).byte().permute(1, 2, 0).contiguous().cpu().numpy()
                    )
                network_gui.send(net_image_bytes, model_args.source_path)
                if (
                    do_training
                ) and (  # keep connection with gui at last iteration
                    # iter_idx == optim_args.iterations at last iteration
                    (iter_idx < int(optim_args.iterations) 
                ) or (
                    not keep_alive)
                ) :
                    break
            except Exception as e:
                network_gui.conn = None

        iter_start.record()

        gaussians.update_learning_rate(iter_idx)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iter_idx % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        if not viewpoint_stack:
            viewpoint_stack = scene.getTrainCameras().copy()
        viewpoint_cam = viewpoint_stack.pop(randint(0, len(viewpoint_stack)-1))

        
        # start debugging from iter_idx reach $debug_from because debuggins is slow
        if (iter_idx - 1) == debug_from:
            pipe_args.debug = True

        render_pkg = render(
            viewpoint_cam, gaussians, pipe_args,
            bg_color = torch.rand((3), device="cuda") if optim_args.random_background else background
        )
        image                   = render_pkg["render"]
        viewspace_point_tensor  = render_pkg["viewspace_points"]
        visibility_filter       = render_pkg["visibility_filter"]
        radii                   = render_pkg["radii"]

        # Loss
        gt_image = viewpoint_cam.original_image.cuda()
        Ll1 = l1_loss(image, gt_image)
        loss = (1.0 - optim_args.lambda_dssim) * Ll1 + optim_args.lambda_dssim * (1.0 - ssim(image, gt_image))
        loss.backward()

        iter_end.record()

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log

            if iter_idx % 10 == 0:
                progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}"})
                progress_bar.update(10)
            if iter_idx == optim_args.iterations:
                progress_bar.close()

            # Log and save
            training_report(
                tb_writer, iter_idx, Ll1, loss, l1_loss,
                iter_start.elapsed_time(iter_end), testing_iterations,
                scene, render, (pipe_args, background)
            )

            if (iter_idx in saving_iterations):
                print("\n[ITER {}] Saving Gaussians".format(iter_idx))
                scene.save(iter_idx)

            # Densification
            if iter_idx < optim_args.densify_until_iter:
                # Keep track of max radii in image-space for pruning
                gaussians.max_radii2D[visibility_filter] = torch.max(
                    gaussians.max_radii2D[visibility_filter],
                    radii[visibility_filter]
                )
                gaussians.add_densification_stats(
                    viewspace_point_tensor,
                    visibility_filter
                )

                if (
                    iter_idx > optim_args.densify_from_iter
                ) and (
                    iter_idx % optim_args.densification_interval == 0
                ):
                    size_threshold = 20 if iter_idx > optim_args.opacity_reset_interval else None
                    gaussians.densify_and_prune(
                        optim_args.densify_grad_threshold, 0.005,
                        scene.cameras_extent, size_threshold
                    )
                
                if (
                    iter_idx % optim_args.opacity_reset_interval == 0
                ) or (
                    model_args.white_background and iter_idx == optim_args.densify_from_iter
                ):
                    gaussians.reset_opacity()

            # Optimizer step
            if iter_idx < optim_args.iterations:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none = True)

            if (iter_idx in checkpoint_iterations):
                print("\n[ITER {}] Saving Checkpoint".format(iter_idx))
                torch.save(
                    (gaussians.capture(), iter_idx),
                    scene.model_path + "/chkpnt" + str(iter_idx) + ".pth"
                )

def prepare_output_and_logger(args):    
    if not args.model_path:
        if os.getenv('OAR_JOB_ID'):
            unique_str=os.getenv('OAR_JOB_ID')
            print("found unique str :", unique_str)
        else:
            unique_str = str(uuid.uuid4())
            print("made unique str :", unique_str)
        args.model_path = os.path.join("./output/", unique_str[0:10])
        
    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok = True)
    print("path exists" if os.path.exists(args.model_path) else "path does not exist")
    with open(os.path.join(args.model_path, "cfg_args"), 'w') as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer

def training_report(
    tb_writer           ,
    iter_idx            ,
    Ll1                 ,
    loss                ,
    l1_loss             ,
    elapsed             ,
    testing_iterations  ,   
    scene               : Scene,
    renderFunc          ,
    renderArgs          ,
):
    if tb_writer:
        tb_writer.add_scalar('train_loss_patches/l1_loss', Ll1.item(), iter_idx)
        tb_writer.add_scalar('train_loss_patches/total_loss', loss.item(), iter_idx)
        tb_writer.add_scalar('iter_time', elapsed, iter_idx)

    # Report test and samples of training set
    if iter_idx in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = (
            {'name': 'test', 'cameras' : scene.getTestCameras()}, 
            {'name': 'train', 'cameras' : [
                scene.getTrainCameras()[idx % len(scene.getTrainCameras())] for idx in range(5, 30, 5)
            ]}
        )

        for config in validation_configs:
            if config['cameras'] and len(config['cameras']) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config['cameras']):
                    image = torch.clamp(
                        renderFunc(viewpoint, scene.gaussians, *renderArgs)["render"], 0.0, 1.0
                    )
                    gt_image = torch.clamp(viewpoint.original_image.to("cuda"), 0.0, 1.0)
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(
                            config['name'] + "_view_{}/render".format(viewpoint.image_name),
                            image[None], global_step=iter_idx
                        )
                        if iter_idx == testing_iterations[0]:
                            tb_writer.add_images(
                                config['name'] + "_view_{}/ground_truth".format(viewpoint.image_name),
                                gt_image[None], global_step=iter_idx
                            )
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config['cameras'])
                l1_test /= len(config['cameras'])          
                print("\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(
                    iter_idx, config['name'], l1_test, psnr_test
                ))
                if tb_writer:
                    tb_writer.add_scalar(
                        config['name'] + '/loss_viewpoint - l1_loss',
                        l1_test, iter_idx
                    )
                    tb_writer.add_scalar(
                        config['name'] + '/loss_viewpoint - psnr',
                        psnr_test, iter_idx
                    )

        if tb_writer:
            tb_writer.add_histogram("scene/opacity_histogram", scene.gaussians.get_opacity, iter_idx)
            tb_writer.add_scalar('total_points', scene.gaussians.get_xyz.shape[0], iter_idx)
        torch.cuda.empty_cache()

if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument('--ip', type=str, default="127.0.0.1")
    parser.add_argument('--port', type=int, default=6009)
    parser.add_argument('--debug_from', type=int, default=-1)
    parser.add_argument('--detect_anomaly', action='store_true', default=False)
    parser.add_argument("--test_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[7_000, 30_000])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default = None)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)
    
    print("Optimizing " + args.model_path)

    # Initialize system state (RNG)
    safe_state(args.quiet)

    # Start GUI server, configure and run training
    print("connecting,", args.ip, args.port)
    network_gui.init(args.ip, args.port)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    training(
        model_args              = lp.extract(args),
        optim_args              = op.extract(args),
        pipe_args               = pp.extract(args),
        testing_iterations      = args.test_iterations,
        saving_iterations       = args.save_iterations,
        checkpoint_iterations   = args.checkpoint_iterations,
        start_checkpoint        = args.start_checkpoint,
        debug_from              = args.debug_from
    )

    # All done
    print("\nTraining complete.")
