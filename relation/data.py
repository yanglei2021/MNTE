import torch
import torch.utils.data as data
import torchvision.transforms as transforms
import os
from PIL import Image
import numpy as np
import json as jsonmod
from transformers import BertTokenizer
import pickle
import tqdm

from pycocotools.coco import COCO



def get_paths(opt):

    name = opt.data_name
    annotations_path = os.path.join(opt.data_path, name, 'captions')
    use_restval = opt.restval

    roots = {}
    ids = {}
    if 'coco' == name:
        imgdir = opt.images_path
        capdir = annotations_path
        roots['train'] = {
            'img': os.path.join(imgdir, 'train2014'),
            'cap': os.path.join(capdir, 'captions_train2014.json')
        }
        roots['val'] = {
            'img': os.path.join(imgdir, 'val2014'),
            'cap': os.path.join(capdir, 'captions_val2014.json')
        }
        roots['test'] = {
            'img': os.path.join(imgdir, 'val2014'),
            'cap': os.path.join(capdir, 'captions_val2014.json')
        }
        roots['trainrestval'] = {
            'img': (roots['train']['img'], roots['val']['img']),
            'cap': (roots['train']['cap'], roots['val']['cap'])
        }
        ids['train'] = np.load(os.path.join(annotations_path, 'coco_train_ids.npy'))
        ids['val'] = np.load(os.path.join(annotations_path, 'coco_dev_ids.npy'))[:5000]
        ids['test'] = np.load(os.path.join(annotations_path, 'coco_test_ids.npy'))
        ids['trainrestval'] = (
            ids['train'],
            np.load(os.path.join(annotations_path, 'coco_restval_ids.npy')))
        if use_restval:
            roots['train'] = roots['trainrestval']
            ids['train'] = ids['trainrestval']
    elif 'f30k' == name:
        imgdir = opt.images_path
        cap = os.path.join(annotations_path, 'dataset_flickr30k.json')
        roots['train'] = {'img': imgdir, 'cap': cap}
        roots['val'] = {'img': imgdir, 'cap': cap}
        roots['test'] = {'img': imgdir, 'cap': cap}
        ids = {'train': None, 'val': None, 'test': None}

    return roots, ids


class CocoDataset(data.Dataset):
    """COCO Custom Dataset compatible with torch.utils.data.DataLoader."""

    def __init__(self, root, json, transform=None, ids=None, get_images=True):
        """
        Args:
            root: image directory.
            json: coco annotation file path.
            transform: transformer for image.
        """
        self.root = root
        self.get_images = get_images
        # when using `restval`, two json files are needed
        if isinstance(json, tuple):
            self.coco = (COCO(json[0]), COCO(json[1]))
        else:
            self.coco = (COCO(json),)
            self.root = (root,)
        # if ids provided by get_paths, use split-specific ids
        if ids is None:
            self.ids = list(self.coco.anns.keys())
        else:
            self.ids = ids

        # if `restval` data is to be used, record the break point for ids
        if isinstance(self.ids, tuple):
            self.bp = len(self.ids[0])
            self.ids = list(self.ids[0]) + list(self.ids[1])
        else:
            self.bp = len(self.ids)
        self.transform = transform

    def __getitem__(self, index):
        """This function returns a tuple that is further passed to collate_fn
        """
        root, caption, img_id, path, image, _ = self.get_raw_item(index, self.get_images)

        if self.transform is not None:
            image = self.transform(image)

        target = caption
        return image, target, index, img_id

    def get_raw_item(self, index, load_image=True):
        if index < self.bp:
            coco = self.coco[0]
            root = self.root[0]
        else:
            coco = self.coco[1]
            root = self.root[1]
        ann_id = self.ids[index]
        caption = coco.anns[ann_id]['caption']
        img_id = coco.anns[ann_id]['image_id']
        img = coco.imgs[img_id]
        img_size = np.array([img['width'], img['height']])
        if load_image:
            path = coco.loadImgs(img_id)[0]['file_name']
            image = Image.open(os.path.join(root, path)).convert('RGB')

            return root, caption, img_id, path, image, img_size
        else:
            return root, caption, img_id, None, None, img_size

    def __len__(self):
        return len(self.ids)


class BottomUpFeaturesDataset:
    def __init__(self, root, json, features_path, split, ids=None, **kwargs):
        # which dataset?
        r = root[0] if type(root) == tuple else root
        r = r.lower()
        if 'coco' in r:
            self.underlying_dataset = CocoDataset(root, json, ids=ids)
        elif 'f30k' in r or 'flickr30k' in r:
            self.underlying_dataset = FlickrDataset(root, json, split)

        # data_path = config['image-model']['data-path']
        self.feats_data_path = os.path.join(features_path, 'bu_att')
        self.box_data_path = os.path.join(features_path, 'bu_box')

    def __getitem__(self, index):
        """This function returns a tuple that is further passed to collate_fn
        """
        root, caption, img_id, _, _, img_size = self.underlying_dataset.get_raw_item(index, load_image=False)
        img_feat_path = os.path.join(self.feats_data_path, '{}.npz'.format(img_id))
        img_box_path = os.path.join(self.box_data_path, '{}.npy'.format(img_id))

        img_feat = np.load(img_feat_path)['feat']
        img_boxes = np.load(img_box_path)

        # normalize boxes
        img_boxes = img_boxes / np.tile(img_size, 2)

        img_feat = torch.Tensor(img_feat)
        img_boxes = torch.Tensor(img_boxes)

        target = caption
        # image = (img_feat, img_boxes)
        return img_feat, img_boxes, target, index, img_id

    def __len__(self):
        return len(self.underlying_dataset)


class FlickrDataset(data.Dataset):
    """
    Dataset loader for Flickr30k and Flickr8k full datasets.
    """

    def __init__(self, root, json, split, transform=None, get_images=True):
        self.root = root
        self.split = split
        self.get_images = get_images
        self.transform = transform
        self.dataset = jsonmod.load(open(json, 'r'))['images']
        self.ids = []
        for i, d in enumerate(self.dataset):
            if d['split'] == split:
                self.ids += [(i, x) for x in range(len(d['sentences']))]

        # dump flickr images sizes on files for later use
        size_file = os.path.join(root, 'sizes.pkl')
        if os.path.isfile(size_file):
            # load it
            with open(size_file, 'rb') as f:
                self.sizes = pickle.load(f)
        else:
            # build it
            sizes = []
            for im in tqdm.tqdm(self.dataset):
                path = im['filename']
                image = Image.open(os.path.join(root, path))
                sizes.append(image.size)

            with open(size_file, 'wb') as f:
                pickle.dump(sizes, f)
            self.sizes = sizes

    def __getitem__(self, index):
        """This function returns a tuple that is further passed to collate_fn
        """
        root, caption, img_id, path, image, _ = self.get_raw_item(index, self.get_images)
        if self.transform is not None:
            image = self.transform(image)

        # Convert caption (string) to word ids.
        target = caption
        return image, target, index, img_id

    def get_raw_item(self, index, load_image=True):
        root = self.root
        ann_id = self.ids[index]
        img_id = ann_id[0]
        caption = self.dataset[img_id]['sentences'][ann_id[1]]['raw']
        img_size = self.sizes[img_id]

        if load_image:
            path = self.dataset[img_id]['filename']
            image = Image.open(os.path.join(root, path)).convert('RGB')
            return root, caption, img_id, path, image, img_size
        else:
            return root, caption, img_id, None, None, img_size

    def __len__(self):
        return len(self.ids)


class Collate:
    def __init__(self, opt):
        self.vocab_type = opt.text_model_name
        if self.vocab_type == 'bert':
            self.tokenizer = BertTokenizer.from_pretrained(opt.bert_text)

    def __call__(self, data):
        """Build mini-batch tensors from a list of (image, caption) tuples.
            Args:
                data: list of (image, caption) tuple.
                    - image: torch tensor of shape (3, 256, 256) or (? > 3, 2048)
                    - caption: torch tensor of shape (?); variable length.

            Returns:
                images: torch tensor of shape (batch_size, 3, 256, 256).
                targets: torch tensor of shape (batch_size, padded_length).
                lengths: list; valid length for each padded caption.
            """
        # Sort a data list by caption length
        # data.sort(key=lambda x: len(x[1]), reverse=True)
        if len(data[0]) == 5:  # TODO: find a better way to distinguish the two
            images, boxes, captions, ids, img_ids = zip(*data)
        elif len(data[0]) == 4:
            images, captions, ids, img_ids = zip(*data)

        preextracted_captions = type(captions[0]) is tuple
        if preextracted_captions:
            # they are pre-extracted features
            captions, cap_features, wembeddings = zip(*captions)
            cap_lengths = [len(cap) for cap in cap_features]
            captions = [torch.LongTensor(c) for c in captions]
            cap_features = [torch.FloatTensor(f) for f in cap_features]
            wembeddings = [torch.FloatTensor(w) for w in wembeddings]
        else:
            if self.vocab_type == 'bert':
                cap_lengths = [len(self.tokenizer.tokenize(c)) + 2 for c in
                               captions]  # + 2 in order to account for begin and end tokens
                max_len = max(cap_lengths)
                captions_ids = [torch.LongTensor(self.tokenizer.encode(c, max_length=max_len, pad_to_max_length=True))
                                for c in captions]

            captions = captions_ids
        # Merge images (convert tuple of 3D tensor to 4D tensor)
        preextracted_images = not (images[0].shape[0] == 3)
        if not preextracted_images:
            # they are images
            images = torch.stack(images, 0)
        else:
            # they are image features, variable length
            feat_lengths = [f.shape[0] + 1 for f in images]  # +1 because the first region feature is reserved as CLS
            feat_dim = images[0].shape[1]
            img_features = torch.zeros(len(images), max(feat_lengths), feat_dim)
            for i, img in enumerate(images):
                end = feat_lengths[i]
                img_features[i, 1:end] = img

            box_lengths = [b.shape[0] + 1 for b in boxes]  # +1 because the first region feature is reserved as CLS
            assert box_lengths == feat_lengths
            out_boxes = torch.zeros(len(boxes), max(box_lengths), 4)
            for i, box in enumerate(boxes):
                end = box_lengths[i]
                out_boxes[i, 1:end] = box

        # Merget captions (convert tuple of 1D tensor to 2D tensor)
        if preextracted_captions:
            captions_t = torch.zeros(len(captions), max(cap_lengths)).long()
            features_t = torch.zeros(len(cap_features), max(cap_lengths), cap_features[0].shape[1])
            wembeddings_t = torch.zeros(len(wembeddings), max(cap_lengths), wembeddings[0].shape[1])
            for i, (cap, feats, wembs, l) in enumerate(zip(captions, cap_features, wembeddings, cap_lengths)):
                captions_t[i, :l] = cap[:l]
                features_t[i, :l] = feats[:l]
                wembeddings_t[i, :l] = wembs[:l]
            targets = (captions_t, features_t, wembeddings_t)
        else:
            targets = torch.zeros(len(captions), max(cap_lengths)).long()
            for i, cap in enumerate(captions):
                end = cap_lengths[i]
                targets[i, :end] = cap[:end]

        if not preextracted_images:
            return images, targets, None, cap_lengths, None, ids
        else:
            # features = features.permute(0, 2, 1)
            return img_features, targets, feat_lengths, cap_lengths, out_boxes, ids


def get_loader_single(data_name, split, root, json, transform, preextracted_root=None,
                      batch_size=100, shuffle=True,
                      num_workers=2, ids=None, collate_fn=None, **kwargs):
    """Returns torch.utils.data.DataLoader for custom coco dataset."""
    if 'coco' in data_name:
        if preextracted_root is not None:
            dataset = BottomUpFeaturesDataset(root=root,
                                              json=json,
                                              features_path=preextracted_root, split=split,
                                              ids=ids, **kwargs)
        else:
            # COCO custom dataset
            dataset = CocoDataset(root=root,
                                  json=json,
                                  transform=transform, ids=ids)
    elif 'f8k' in data_name or 'f30k' in data_name:
        if preextracted_root is not None:
            dataset = BottomUpFeaturesDataset(root=root,
                                              json=json,
                                              features_path=preextracted_root, split=split,
                                              ids=ids, **kwargs)
        else:
            dataset = FlickrDataset(root=root,
                                    split=split,
                                    json=json,
                                    transform=transform)

    # Data loader
    data_loader = torch.utils.data.DataLoader(dataset=dataset,
                                              batch_size=batch_size,
                                              shuffle=shuffle,
                                              pin_memory=True,
                                              num_workers=num_workers,
                                              collate_fn=collate_fn)
    return data_loader


def get_transform(data_name, split_name, config):
    normalizer = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                      std=[0.229, 0.224, 0.225])
    t_list = []
    # if split_name == 'train':
    #     t_list = [transforms.RandomResizedCrop(config['image-model']['crop-size']),
    #               transforms.RandomHorizontalFlip()]
    # elif split_name == 'val':
    #     t_list = [transforms.Resize(256), transforms.CenterCrop(224)]
    # elif split_name == 'test':
    #     t_list = [transforms.Resize(256), transforms.CenterCrop(224)]

    t_end = [transforms.ToTensor(), normalizer]
    transform = transforms.Compose(t_list + t_end)
    return transform


def get_loaders(opt, batch_size=None):
    data_name = opt.data_name
    if batch_size is None:
        batch_size = opt.bs
    collate_fn = Collate(opt)
    roots, ids = get_paths(opt)

    transform = get_transform(data_name, 'train', opt)
    preextracted_root = opt.pre_extracted_features_root

    train_loader = get_loader_single(data_name, 'train',
                                     roots['train']['img'],
                                     roots['train']['cap'],
                                     transform, ids=ids['train'],
                                     preextracted_root=preextracted_root,
                                     batch_size=batch_size, shuffle=True,
                                     num_workers=opt.workers,
                                     collate_fn=collate_fn, opt=opt)

    transform = get_transform(data_name, 'val', opt)
    val_loader = get_loader_single(data_name, 'val',
                                   roots['val']['img'],
                                   roots['val']['cap'],
                                   transform, ids=ids['val'],
                                   preextracted_root=preextracted_root,
                                   batch_size=batch_size, shuffle=False,
                                   num_workers=opt.workers,
                                   collate_fn=collate_fn, opt=opt)

    return train_loader, val_loader


def get_test_loader(opt, workers, split_name='test', batch_size=None):
    data_name = opt.data_name
    if batch_size is None:
        batch_size = opt.bs
    collate_fn = Collate(opt)
    # Build Dataset Loader
    roots, ids = get_paths(opt)

    preextracted_root = opt.pre_extracted_features_root

    transform = get_transform(data_name, split_name, opt)
    test_loader = get_loader_single(data_name, split_name,
                                    roots[split_name]['img'],
                                    roots[split_name]['cap'],
                                    transform, ids=ids[split_name],
                                    preextracted_root=preextracted_root,
                                    batch_size=batch_size, shuffle=False,
                                    num_workers=workers,
                                    collate_fn=collate_fn, opt=opt)
    return test_loader