#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2019 Theo Morales <theo.morales.fr@gmail.com>
#
# Distributed under terms of the GPLv3 license.

"""
DatasetFactory

Generates a given number of images by projecting a given model in random
positions, onto randomly selected background images from the given dataset.
"""

import multiprocessing.dummy as mp
import numpy as np
import argparse
import cv2
import sys
import os

from tqdm import *
from PIL import Image, ImageDraw
from skimage.util import random_noise
from scene_renderer import SceneRenderer
from dataset import Dataset, AnnotatedImage, SyntheticAnnotations


'''
                ----- TODO -----

[x] Thread it!
[x] Random positioning of the gate
[x] Boundaries definition for the gate (relative to the mesh's size)
[x] Compute the center of the gate
[x] Compute the presence of the gate in the image frame
[x] Convert world coordinates to image coordinates
[x] Compute the distance to the gate
[x] Perspective projection for visualization
[x] Camera calibration (use the correct parameters)
[x] Project on transparent background
[x] Overlay with background image
[x] Model the camera distortion
[x] Save generated dataset online in a separate thread
[x] Add background gates
[x] Compute gate visibility percentage over the whole dataset
[x] Compute gate orientation with respect to the camera
[x] Ensure that the gate is always oriented towards the camera (for the
    annotation)
[x] Save annotations
[ ] Refactor DatasetFactory (create augentation class)
[ ] Refactor SceneRenderer (use an interface to let users script their scene)
[ ] Apply the distortion to the OpenGL projection
[x] Add variation to the mesh and texture
[x] Motion blur
[x] Anti alisasing
[x] Ship it!

'''


class DatasetFactory:
    def __init__(self, args):
        self.meshes_dir = args.meshes_dir
        self.nb_threads = args.threads
        self.count = args.nb_images
        self.cam_param = args.camera_parameters
        self.verbose = args.verbose
        self.extra_verbose = args.extra_verbose
        self.max_blur_amount = args.blur_threshold
        self.noise_amount = args.noise_amount
        self.no_blur = args.no_blur
        self.seed = args.seed
        self.max_gates = args.max_gates
        self.min_dist = args.min_dist
        if self.extra_verbose:
            self.verbose = True
        self.background_dataset = Dataset(args.dataset, args.seed)
        if not self.background_dataset.load(self.count,
                                            os.path.join(args.dataset,
                                                         'annotations.csv')):
            print("[!] Could not load dataset!")
            sys.exit(1)
        self.generated_dataset = Dataset(args.destination, max=100)
        self.base_width, self.base_height = self.background_dataset.get_image_size()
        self.target_width, self.target_height = [
            int(x) for x in args.resolution.split('x')]
        self.sample_no = 0
        self.visible_gates = 0

    def set_world_parameters(self, boundaries):
        self.world_boundaries = boundaries

    def run(self):
        print("[*] Generating dataset...")
        print("[*] Using {}x{} target resolution".format(self.target_width,
                                                         self.target_height))
        save_thread = mp.threading.Thread(target=self.generated_dataset.save)
        projector = SceneRenderer(self.meshes_dir, self.base_width,
                                  self.base_height, self.world_boundaries,
                                  self.cam_param, self.extra_verbose,
                                  self.seed)
        save_thread.start()
        for i in tqdm(range(self.count),
                      unit="img",
                      bar_format="{l_bar}{bar}|{n_fmt}/{total_fmt}"):
            self.generate(i, projector)

        self.generated_dataset.data.put(None)
        save_thread.join()
        print("[*] Saved to {}".format(self.generated_dataset.path))
        print("[*] Gate visibilty percentage: {}%".format(
            int((self.visible_gates/self.count)*100)))

    '''
    FIXME: Memory leaks all over... Not easy to reuse a projector per thread.
    '''
    def run_multi_threaded(self):
        print("[*] Generating dataset...")
        print("[*] Using {}x{} target resolution".format(self.target_width,
                                                         self.target_height))
        save_thread = mp.threading.Thread(target=self.generated_dataset.save)

        with mp.Pool(self.nb_threads) as p:
            max_ = self.count
            with tqdm(total=max_) as pbar:
                save_thread.start()
                self.projectors = []
                for i in range(self.nb_threads):
                    self.projectors.append(
                        SceneRenderer(self.meshes_dir, self.base_width,
                                      self.base_height, self.world_boundaries,
                                      self.cam_param, self.extra_verbose,
                                      self.seed))
                args = zip(range(max_), max_ * list(range(self.nb_threads)))
                for i, _ in tqdm(
                        enumerate(p.imap_unordered(self.generate, args))):
                    pbar.update()
                p.close()
                p.join()
                self.generated_dataset.data.put(None)
                save_thread.join()
                print("[*] Saved to {}".format(self.generated_dataset.path))
                print("[*] Gate visibilty percentage: {}%".format(
                    int((self.visible_gates/self.count)*100)))

    def generate(self, index, projector):
        background = self.background_dataset.get()
        projector.set_drone_pose(background.annotations)
        projection, annotations = projector.generate(min_dist=self.min_dist,
                                                     max_gates=self.max_gates)
        bboxes = annotations['bboxes']
        gate_visible = len(bboxes) > 0

        if gate_visible:
            projection_blurred = self.apply_motion_blur(
                projection, amount=self.get_blur_amount(background.image()))
            projection_noised = self.add_noise(projection_blurred)
            projection = projection_noised
            self.visible_gates += 1

        output = self.combine(projection, background.image())

        # TODO: Refactor (one-liner?)
        scaled_bboxes = []
        for bbox in bboxes:
            scaled_bbox = bbox
            for key, val in bbox.items():
                if key in ['min', 'max']:
                    scaled_bbox[key] = self.scale_coordinates(val, output.size)
                elif key == 'normal' and bbox['class_id'] != 2:
                    scaled_bbox[key]['origin'] = self.scale_coordinates(
                        val['origin'], output.size)
                    scaled_bbox[key]['end'] = self.scale_coordinates(
                        val['end'], output.size)
            scaled_bboxes.append(scaled_bbox)

        if self.verbose:
            if gate_visible:
                self.draw_bounding_boxes(output, scaled_bboxes,
                                         annotations['closest_gate'])
                self.draw_normals(output, scaled_bboxes)

        if self.extra_verbose:
            self.draw_image_annotations(output, annotations)

        self.generated_dataset.put(
            AnnotatedImage(
                output,
                index,
                SyntheticAnnotations(scaled_bboxes)))

    # Scale to target width/height
    def scale_coordinates(self, coordinates, target_coordinates):
        coordinates[0] = int(coordinates[0]
                          * target_coordinates[0]
                          / self.base_width)
        coordinates[1] = int(coordinates[1]
                          * target_coordinates[1]
                          / self.base_height)

        return coordinates

    # NB: Thumbnail() only scales down!!
    def combine(self, projection: Image, background: Image):
        background = background.convert('RGBA')
        if projection.size != (self.base_width, self.base_height):
            projection.thumbnail(
                (self.base_width, self.base_height),
                Image.ANTIALIAS)
        output = Image.alpha_composite(background, projection)
        if output.size != (self.target_width, self.target_height):
            output.thumbnail(
                (self.target_width, self.target_height),
                Image.ANTIALIAS)

        return output

    def get_blur_amount(self, img: Image):
        gray_scale = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        variance_of_laplacian = cv2.Laplacian(gray_scale, cv2.CV_64F).var()
        blur_amount = variance_of_laplacian / self.max_blur_amount
        if blur_amount > 1:
            blur_amount = 0.9

        return 1 - blur_amount

    def add_noise(self, img):
        noisy_img = random_noise(img, mode='gaussian',
                                 var=self.noise_amount**2)
        noisy_img = (255*noisy_img).astype(np.uint8)

        return Image.fromarray(noisy_img)

    def apply_motion_blur(self, img: Image, amount=0.5):
        cv_img = np.array(img)

        if self.no_blur:
            return cv_img

        if amount <= 0.3:
            size = 3
        elif amount <= 0.7:
            size = 5
        else:
            size = 9
        kernel = np.identity(size)
        kernel /= size

        return cv2.filter2D(cv_img, -1, kernel)

    def draw_bounding_boxes(self, img, bboxes, closest_gate, color="yellow",
                            closest_color="green"):
        gate_draw = ImageDraw.Draw(img)
        for i, bbox in enumerate(bboxes):
            c = color
            if closest_gate is not None and i == closest_gate:
                c = closest_color
            gate_draw.rectangle([(bbox['min'][0], bbox['min'][1]),
                                 (bbox['max'][0], bbox['max'][1])],
                                outline=c, width=3)

    def draw_normals(self, img, bboxes):
        for bbox in bboxes:
            if bbox['class_id'] != 2:
                self.draw_gate_normal(img, bbox['normal']['origin'],
                                      bbox['normal']['end'])

    def draw_gate_normal(self, img, center, normal_gt, color="red"):
        gate_draw = ImageDraw.Draw(img)
        gate_draw.line((center[0], center[1], normal_gt[0], normal_gt[1]),
                       fill=color, width=2)

    def draw_image_annotations(self, img, annotations, color="green"):
        text = "\ngate_distance: {}\ngate_rotation:\ {}\ndrone_pose:\
                {}\ndrone_orientation:{}".format(
                    annotations['gate_distance'],
                    annotations['gate_rotation'],
                    annotations['drone_pose'],
                    annotations['drone_orientation'])
        text_draw = ImageDraw.Draw(img)
        text_draw.text((0, 0), text, color)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='Generate a hybrid synthetic dataset of projections of a \
        given 3D model, in random positions and orientations, onto randomly \
        selected background images from a given dataset.')
    parser.add_argument('meshes_dir', help='the 3D meshes directory containing'
                        ' the models to project (along with textures)',
                        type=str)
    parser.add_argument('dataset', help='the path to the background images \
                        dataset', type=str)
    parser.add_argument('destination', metavar='dest', help='the path\
                        to the destination folder for the generated dataset',
                        type=str)
    parser.add_argument('--count', dest='nb_images', default=5, type=int,
                        help='the number of images to be generated')
    parser.add_argument('--res', dest='resolution', default='640x480',
                        type=str, help='the desired resolution (WxH)')
    parser.add_argument('-t', dest='threads', default=4, type=int,
                        help='the number of threads to use')
    parser.add_argument('--camera', dest='camera_parameters', type=str,
                        help='the path to the camera parameters YAML file\
                        (output of OpenCV\'s calibration)',
                        required=True)
    parser.add_argument('-v', dest='verbose', help='verbose output',
                        action='store_true', default=False)
    parser.add_argument('-vv', dest='extra_verbose', help='extra verbose\
                        output (render the perspective grid)',
                        action='store_true', default=False)
    parser.add_argument('--seed', dest='seed', default=None,
                        help='use a fixed seed')
    parser.add_argument('--blur', dest='blur_threshold', default=200, type=int,
                        help='the blur threshold')
    parser.add_argument('--noise', dest='noise_amount', default=0.015,
                        type=float, help='the gaussian noise amount')
    parser.add_argument('--no-blur', dest='no_blur', action='store_true',
                        default=False, help='disable synthetic motion blur')
    parser.add_argument('--max-gates', dest='max_gates', type=int, help='the\
                        maximum amount of gates to spawn', default=6)
    parser.add_argument('--min-dist', dest='min_dist', type=float, help='the\
                        minimum distance between each gate, in meter',
                        default=3.5)

    datasetFactory = DatasetFactory(parser.parse_args())
    # Real world boundaries in meters (relative to the mesh's scale)
    datasetFactory.set_world_parameters(
        {'x': 10, 'y': 10},
    )
    datasetFactory.run()
