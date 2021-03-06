# -*- coding: utf-8 -*-
import contextlib
import io
import os
import os.path as osp
import pickle
import copy
import random

import numpy as np
from PIL import Image
from pycocotools import mask as MaskApi
from pycocotools.coco import COCO

import torch
from torch.utils.data import Dataset


COCO_ROOT = '/public/home/jm/Data/datasets/COCO'
CACHE_ROOT = '/public/home/jm/Data/output/stm_output'
MAX_TRAINING_OBJ = 6


class BaseData(Dataset):
    def __init__(self,):
        self.max_skip = None

    def increase_max_skip(self, ):
        pass

    def set_max_skip(self, max_skip):
        self.max_skip = max_skip

class COCODataset(BaseData):
    r"""
    COCO dataset helper
    dataset_root: str
        path to root of the dataset
    subsets: list
        dataset split name [train2017,val2017]
    """
    data_items = []

    def __init__(self, transform=None, sampled_frames=3, ratio=0.1) -> None:
        r"""
        Create dataset with config
        """
        super(COCODataset, self).__init__()
        self.dataset_root = COCO_ROOT
        self.subsets = ["train2017"]
        self.sampled_frames = sampled_frames
        self.transform = transform
        self.train = True
        self.ratio = ratio

        if len(COCODataset.data_items) == 0:
            self._ensure_cache()

    def _generate_mask_from_anno(self, raw_mask, img_h, img_w):
        jth_mask_raw = MaskApi.frPyObjects(raw_mask, img_h, img_w)
        jth_mask = MaskApi.decode(jth_mask_raw)
        mask_shape = jth_mask.shape
        if len(mask_shape) == 3:
            target_mask = np.zeros((mask_shape[0], mask_shape[1]),
                                   dtype=np.uint8)
            for iter_chl in range(mask_shape[2]):
                target_mask = target_mask | jth_mask[:, :, iter_chl]
        else:
            target_mask = jth_mask
        target_mask = target_mask.astype(np.uint8) # 0 or 1
        return target_mask

    def __getitem__(self, item):
        """
        :param item: int, video id
        :return:
            image_files
            annos
            meta (optional)
        """
        num_obj = 0
        while num_obj==0:
            idx = random.sample(range(len(COCODataset.data_items)), 1)[0]
            record = COCODataset.data_items[item]
            image_file = record["file_name"]
            img_h = record["height"]
            img_w = record["width"]
            anno = record['annotations']
            mask_anno = []
            for obj in anno:
                raw_mask = obj['segmentation']
                mask_obj = self._generate_mask_from_anno(raw_mask, img_h, img_w)
                mask_anno.append(mask_obj)
            num_obj = len(mask_anno)

        frame = np.array(Image.open(image_file))
        if len(frame.shape)==2:
            frame = frame[:, :, np.newaxis]
            frame = frame.repeat(3, axis=2)
        assert len(frame.shape) == 3
        mask = np.stack(mask_anno, axis=2)
        # add background
        bg = np.ones(mask.shape[:2]+(1,))
        bg[np.any(mask==1, axis=2)] = 0
        mask = np.concatenate((bg, mask), axis=2)

        frames = [frame.copy() for i in range(self.sampled_frames)]
        masks = [mask.copy() for i in range(self.sampled_frames)]

        if self.transform is None:
            raise RuntimeError('Lack of proper transformation')
        frames, masks = self.transform(frames, masks, True)

        if self.train:
            num_obj = 0
            for i in range(1, MAX_TRAINING_OBJ+1):
                if torch.sum(masks[0, i]) > 0:
                    num_obj += 1
                else:
                    break

        return frames, masks, num_obj, None

    def __len__(self):
        return int(self.ratio*len(COCODataset.data_items))

    def _ensure_cache(self):
        dataset_root = self.dataset_root
        subsets = self.subsets
        for subset in subsets:
            data_anno_list = []
            image_root = osp.join(dataset_root, "images", subset)
            cache_file = osp.join(CACHE_ROOT,"coco_mask_{}.pkl".format(subset))
            # print(cache_file)
            if osp.exists(cache_file):
                with open(cache_file, 'rb') as f:
                    COCODataset.data_items += pickle.load(f)
                print("==> {}: loaded cache file {}".format(
                    COCODataset.__name__, cache_file))
            else:
                anno_file = osp.join(
                    dataset_root,
                    "annotations",
                    "instances_{}.json".format(subset))
                with contextlib.redirect_stdout(io.StringIO()):
                    coco_api = COCO(anno_file)
                    # sort indices for reproducible results
                    img_ids = sorted(coco_api.imgs.keys())
                    # imgs is a list of dicts, each looks something like:
                    # {'license': 4,
                    #  'url': 'http://farm6.staticflickr.com/5454/9413846304_881d5e5c3b_z.jpg',
                    #  'file_name': 'COCO_val2014_000000001268.jpg',
                    #  'height': 427,
                    #  'width': 640,
                    #  'date_captured': '2013-11-17 05:57:24',
                    #  'id': 1268}
                    imgs = coco_api.loadImgs(img_ids)
                    # anns is a list[list[dict]], where each dict is an annotation
                    # record for an object. The inner list enumerates the objects in an image
                    # and the outer list enumerates over images. Example of anns[0]:
                    # [{'segmentation': [[192.81,
                    #     247.09,
                    #     ...
                    #     219.03,
                    #     249.06]],
                    #   'area': 1035.749,
                    #   'iscrowd': 0,
                    #   'image_id': 1268,
                    #   'bbox': [192.81, 224.8, 74.73, 33.43],
                    #   'category_id': 16,
                    #   'id': 42986},
                    #  ...]
                    anns = [coco_api.imgToAnns[img_id] for img_id in img_ids]

                if "minival" not in anno_file:
                    # The popular valminusminival & minival annotations for COCO2014 contain this bug.
                    # However the ratio of buggy annotations there is tiny and does not affect accuracy.
                    # Therefore we explicitly white-list them.
                    ann_ids = [
                        ann["id"] for anns_per_image in anns
                        for ann in anns_per_image
                    ]
                    assert len(set(ann_ids)) == len(
                        ann_ids
                    ), "Annotation ids in '{}' are not unique!".format(
                        anno_file)

                imgs_anns = list(zip(imgs, anns))
                ann_keys = ["iscrowd", "bbox", "keypoints", "category_id"]
                # iterate over annotation
                for (img_dict, anno_dict_list) in imgs_anns:
                    record = {}
                    record["file_name"] = os.path.join(image_root,
                                                       img_dict["file_name"])
                    record["height"] = img_dict["height"]
                    record["width"] = img_dict["width"]
                    image_id = record["image_id"] = img_dict["id"]

                    objs = []
                    for anno in anno_dict_list:
                        # Check that the image_id in this annotation is the same as
                        # the image_id we're looking at.
                        # This fails only when the data parsing logic or the annotation file is buggy.

                        # The original COCO valminusminival2014 & minival2014 annotation files
                        # actually contains bugs that, together with certain ways of using COCO API,
                        # can trigger this assertion.
                        assert anno["image_id"] == image_id, print(
                            "==> {} vs {}".format(anno["image_id"], image_id))

                        assert anno.get(
                            "ignore", 0
                        ) == 0, '"ignore" in COCO json file is not supported.'

                        obj = {
                            key: anno[key]
                            for key in ann_keys if key in anno
                        }

                        segm = anno.get("segmentation", None)
                        if segm:  # either list[list[float]] or dict(RLE)
                            if not isinstance(segm, dict):
                                # filter out invalid polygons (< 3 points)
                                segm = [
                                    poly for poly in segm
                                    if len(poly) % 2 == 0 and len(poly) >= 6
                                ]
                                if len(segm) == 0:
                                    continue  # ignore this instance
                            obj["segmentation"] = segm
                        else:
                            continue
                        objs.append(obj)
                    # filter out image without any targets
                    if len(objs) == 0:
                        continue
                    record["annotations"] = objs
                    data_anno_list.append(record)

                # save internal .json file
                cache_dir = osp.dirname(cache_file)
                if not osp.exists(cache_dir):
                    os.makedirs(cache_dir)
                with open(cache_file, 'wb') as f:
                    pickle.dump(data_anno_list, f)
                print("==> COCO dataset: cache dumped at: {}".format(cache_file))
                COCODataset.data_items += data_anno_list
