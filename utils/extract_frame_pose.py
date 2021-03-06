#!/usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2019 Theo Morales <theo.morales.fr@gmail.com>
#
# Distributed under terms of the MIT license.


'''
Utility script to extract frames and drone poses from the given topics
(usually used with rosbag).
It generates a CSV file containing annotations for each extracted frame, after
synchronizing the timestamps.
'''

import os
import sys
import cv2
import rospy
import cv_bridge
import message_filters

from tqdm import tqdm
from collections import OrderedDict
from geometry_msgs.msg import TransformStamped
from sensor_msgs.msg import CompressedImage, Image

TIME_STAMP_MAX_DIFF = 5

class FramePoseExtractor():
    def __init__(self):
        self.output_path = rospy.get_param('~output', 'extracted_output/')
        self.csv_path = rospy.get_param('~filename', 'annotations.csv')
        self.do_dynamic_scaling = rospy.get_param('~do_dynamic_scaling', False)
        self.raw = rospy.get_param('~raw', False)
        self.pose_topic = rospy.get_param('~poses', None)
        self.img_topic = rospy.get_param('~images', None)
        self.first_second = None
        self.annotations = OrderedDict()
        self.images = OrderedDict()

        if self.pose_topic is None:
            rospy.logwarn("""FramePoseExtractor: rosparam '~poses' has not been specified!
Typical command-line usage:
      $ ./extract_frame_pose _poses:=<pose_topic> _images:=<image_topic> _output:=<output_path> _raw:=<1|0>'""")
            sys.exit(1)

        if self.img_topic is None:
            rospy.logwarn("""FramePoseExtractor: rosparam '~images' has not been specified!
Typical command-line usage:
      $ ./extract_frame_pose _poses:=<pose_topic> _images:=<image_topic> _output:=<output_path> _raw:=<1|0>'""")
            sys.exit(1)

    def extract(self):
        if not os.path.isdir(self.output_path):
            os.mkdir(self.output_path)
        self.csv = open(os.path.join(self.output_path,
                                     self.csv_path), 'w')
        # Header
        self.csv.write("frame,translation_x,translation_y,translation_z,rotation_x,rotation_y,rotation_z,rotation_w,timestamp\n")
        poses_sub = [message_filters.Subscriber(self.pose_topic, TransformStamped)]
        if self.raw:
            imgs_sub = [message_filters.Subscriber(self.img_topic, Image)]
        else:
            imgs_sub = [message_filters.Subscriber(self.img_topic, CompressedImage)]

        if rospy.get_param('~approximate_sync', False):
            print('[*] Using approximate sync')
            poses_sync = message_filters.ApproximateTimeSynchronizer(
                poses_sub, queue_size=1000, slop=.1)
            imgs_sync = message_filters.ApproximateTimeSynchronizer(
                imgs_sub, queue_size=1000, slop=.1)
        else:
            poses_sync = message_filters.TimeSynchronizer(
                poses_sub, queue_size=1000)
            imgs_sync = message_filters.TimeSynchronizer(
                imgs_sub, queue_size=1000)

        poses_sync.registerCallback(self._save_poses)
        if self.raw:
            imgs_sync.registerCallback(self._save_images)
        else:
            imgs_sync.registerCallback(self._save_compressed_images)
        print("[*] Extracting...")
        raw_input("[*] Press Enter when the stream is complete...")
        self._write_csv()

    def _save_compressed_images(self, *img_messages):
        for i, img_msg in enumerate(img_messages):
            if not self.first_second:
                self.first_second = img_msg.header.stamp.secs
            seconds = img_msg.header.stamp.secs - self.first_second
            nanoseconds = img_msg.header.stamp.nsecs
            fname = "%04d_%09d.jpg" % (seconds, nanoseconds)
            self.images[fname] = (1000000000 * seconds) + nanoseconds
            with open(os.path.join(self.output_path, fname), 'w') as img_file:
                img_file.write(img_msg.data)

    def _save_raw_images(self, *img_messages):
        bridge = cv_bridge.CvBridge()
        for i, img_msg in enumerate(img_messages):
            if not self.first_second:
                self.first_second = img_msg.header.stamp.secs
            img = bridge.imgmsg_to_cv2(img_msg, desired_encoding="8UC3")
            channels = img.shape[2] if img.ndim == 3 else 1
            encoding_in = bridge.dtype_with_channels_to_cvtype2(img.dtype, channels)
            img = cv_bridge.cvtColorForDisplay(
                img, encoding_in="rgb8", encoding_out='',
                do_dynamic_scaling=self.do_dynamic_scaling)
            seconds = img_msg.header.stamp.secs - self.first_second
            nanoseconds = img_msg.header.stamp.nsecs
            fname = "%04d_%09d.jpg" % (seconds, nanoseconds)
            self.images[fname] = (1000000000 * seconds) + nanoseconds
            cv2.imwrite(fname, img)

    def _save_poses(self, *pose_msgs):
        for i, pose_msg in enumerate(pose_msgs):
            if not self.first_second:
                self.first_second = pose_msg.header.stamp.secs
            timestamp = (1000000000 * (pose_msg.header.stamp.secs - self.first_second)) + pose_msg.header.stamp.nsecs
            image_name = "%04d_%09d.jpg" % (pose_msg.header.stamp.secs -
                                            self.first_second,
                                            pose_msg.header.stamp.nsecs)
            translation = pose_msg.transform.translation
            rotation = pose_msg.transform.rotation
            self.annotations[image_name] = {
                'stamp': timestamp,
                'stamp_readable': image_name.split('.')[0],
                'translation': translation,
                'rotation': rotation
            }

    '''
    Synchronizes the image timestamps with the pose timestamps, and writes the
    correct image-pose matches to a CSV file.
    '''
    # TODO: Fix the first few frames' sync
    def _write_csv(self):
        print("[*] Synchronizing timestamps...")
        # Crop the images to avoid lag issues
        del self.images[self.images.keys()[0]]
        del self.images[self.images.keys()[-1]]

        for img_name, img_timestamp in tqdm(self.images.items()):
            i = 0
            candidate_found = False
            for pose_name, pose in self.annotations.items():
                if pose['stamp'] > img_timestamp:
                    if i > 0:
                        prev_pose = self.annotations[self.annotations.keys()[i-1]]
                        if int((img_timestamp - prev_pose['stamp'])/1000000) <= TIME_STAMP_MAX_DIFF:
                            translation = prev_pose['translation']
                            rotation = prev_pose['rotation']
                            stamp_readable = prev_pose['stamp_readable']
                        else:
                            translation = pose['translation']
                            rotation = pose['rotation']
                            stamp_readable = pose['stamp_readable']
                    else:
                        translation = pose['translation']
                        rotation = pose['rotation']
                        stamp_readable = pose['stamp_readable']

                    candidate_found = True
                    self.csv.write("{},{},{},{},{},{},{},{},{}\n".format(
                        img_name, translation.x, translation.y, translation.z,
                        rotation.x, rotation.y, rotation.z, rotation.w,
                        stamp_readable
                    ))
                i += 1
                if candidate_found:
                    self.annotations = OrderedDict(self.annotations.items()[i::])
                    break

        print("[*] Done. Annotations written to: {}".format(os.path.join(self.output_path,
                                     self.csv_path)))
        self.csv.close()
        sys.exit(0)



if __name__ == '__main__':
    rospy.init_node('extract_frame_pose')
    extractor = FramePoseExtractor()
    extractor.extract()
    rospy.spin()
