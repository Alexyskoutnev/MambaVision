from torch.utils.data import DataLoader
from torchvision import transforms
import transformers
import torch
import torch.nn as nn
from PIL import Image, ImageDraw, ImageFont
from timm.models import create_model
from timm.models.vision_transformer import VisionTransformer, _cfg
from MambaVision.models.mamba.models_mamba import VisionMamba
import os
import time
import uuid

from MambaVision.dataset import OpenImagesDataset, OpenImagesDatasetYolo
from MambaVision.utils.utils import *
from MambaVision.models.mamba.Mamba_bbox import VisionMambaBBox, BBoxLoss
from MambaVision.utils.metrics import calculate_metrics, preprocess_yolo_labels, preprocess_vit_output, preprocess_yolo_output
import yolov5
from yolov5.models.common import AutoShape


DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
IMG_DIR = 'imgs'

def display_image(x, output_dir="imgs"):
    if not os.path.exists(IMG_DIR):
        os.makedirs(output_dir)
    if "raw" in output_dir:
        image_id = str(uuid.uuid4())
        for i in range(x.size(0)):
            img = transforms.ToPILImage()(x[i])
            img.save(os.path.join(IMG_DIR, f'image_{image_id}_{i}.jpg'))
    elif "predictions" in output_dir:
        for i in range(x.size(0)):
            x.save(os.path.join(IMG_DIR, f'image_{i}.jpg'))
            # img = Image.fromarray(x[i].mul(255).permute(1, 2, 0).byte().cpu().numpy())
            # img.save(os.path.join(IMG_DIR, f'image_{i}.jpg'))

def display_bounding_box_image(images, all_ground_truths, all_predictions):
    for i, (image, ground_truth, prediction) in enumerate(zip(images, all_ground_truths, all_predictions)):
        try: 
            if isinstance(image, torch.Tensor):
                image = image.squeeze(0)
                img = Image.fromarray(image.mul(255).permute(1, 2, 0).byte().cpu().numpy())
            elif isinstance(image, tuple):
                img = image[0]
                img = Image.open(img)
            draw = ImageDraw.Draw(img)
            for pred_label in ground_truth:
                xmin = pred_label.xmin.item()
                ymin = pred_label.ymin.item()
                xmax = pred_label.xmax.item()
                ymax = pred_label.ymax.item()
                box = [xmin, ymin, xmax, ymax]
                label_text = str(pred_label.label_class[0])
                draw.text((xmin + 10, ymin + 10), label_text, fill='green')
                draw.rectangle([box[0], box[1], box[2], box[3]], outline='green')
            for pred_label in prediction:
                try:
                    if pred_label.label_class == 'car':
                        xmin = pred_label.xmin
                        ymin = pred_label.ymin
                        xmax = pred_label.xmax
                        ymax = pred_label.ymax
                        box = [xmin, ymin, xmax, ymax]
                        label_text = str(pred_label.label_class)
                        draw.text((xmin + 10, ymin + 10), label_text, fill='red')
                        draw.rectangle([box[0], box[1], box[2], box[3]], outline='red')
                except:
                    continue
            img.save(os.path.join(IMG_DIR, f'image_{i}.jpg'))
        except:
            continue

def test_yolo(yolo, test_dataset, save_predicted_img=False):
    for f in os.listdir(IMG_DIR):
        os.remove(os.path.join(IMG_DIR, f))
    yolo.eval()
    yolo.to(DEVICE)

    all_predictions = []
    all_ground_truths = []
    images_list = []

    for images, targets in test_dataset:
        with torch.no_grad():
            predictions = yolo(images)

        all_predictions.append(predictions)
        all_ground_truths.append(targets)
        images_list.append(images)
    

    precision, recall, mAP = calculate_metrics(all_predictions, all_ground_truths)
    print(f"Precision: {precision}, Recall: {recall}, mAP: {mAP}")

    if save_predicted_img:
        all_ground_truths_post = preprocess_yolo_labels(all_ground_truths)
        all_predictions_post = preprocess_yolo_output(all_predictions, all_ground_truths_post)
        display_bounding_box_image(images_list, all_ground_truths, all_predictions_post)

def test_vit(model, test_dataset, save_predicted_img=False):
    for f in os.listdir(IMG_DIR):
        os.remove(os.path.join(IMG_DIR, f))

    all_predictions = []
    all_ground_truths = []
    images_list = []

    for images, targets, original_img in test_dataset:
        with torch.no_grad():
            predictions = model(images.to(DEVICE))
            img_w = targets[0].orig_w.item()
            img_h = targets[0].orig_h.item()
            predictions['pred_boxes'] = predictions['pred_boxes'] * torch.tensor([img_w, img_h, img_w, img_h], device=DEVICE)
        all_predictions.append(predictions)
        all_ground_truths.append(targets)
        images_list.append(original_img)

    precision, recall, mAP = calculate_metrics(all_predictions, all_ground_truths)
    print(f"Precision: {precision}, Recall: {recall}, mAP: {mAP}")

    if save_predicted_img:
        all_predictions_post = preprocess_vit_output(all_predictions, preprocess_yolo_labels(all_ground_truths)) 
        display_bounding_box_image(images_list, all_ground_truths, all_predictions_post)

def train_mamba(model, test_dataset, config=None, epochs=1000):  

    for f in os.listdir(IMG_DIR):
        os.remove(os.path.join(IMG_DIR, f))

    images_list = []
    loss_fn = nn.SmoothL1Loss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    
    for epoch in range(epochs):
        loss_t = 0.0
        all_predictions = []
        all_ground_truths = []
        for images, targets, original_img in test_dataset:
            optimizer.zero_grad()
            img_w, img_h = targets[0].orig_w.item(), targets[0].orig_h.item()
            class_pred, bbox_pred = model(images.to(DEVICE)) 
            class_pred_2 = torch.argmax(class_pred.squeeze(0)).item()
            class_pred_label = mamba_num_to_class(class_pred_2)
            predictions_label = bounding_box_to_labels(bbox_pred, class_pred_label, img_w, img_h, device=DEVICE)
            gt_t = bounding_box_tensor(targets, device=DEVICE)
            target_label_num = CLASSES_2_NUM[targets[0].label_class[0]]
            loss = BBoxLoss()(bbox_pred, gt_t, class_pred, target_label_num, device=DEVICE)
            loss.backward()
            optimizer.step()
            loss_t += loss.item()
            all_predictions.append(predictions_label)
            all_ground_truths.append(targets)
        
        print(f"Epoch: {epoch}, Loss: {loss_t}")
        precision, recall, mAP = calculate_metrics(all_predictions, all_ground_truths)
        print(f"Precision: {precision}, Recall: {recall}, mAP: {mAP}")

def load_mamba_model(num_classes=1):
    model = create_model(
        'deit_base_patch16_224',
        pretrained=True,
        num_classes=100,
        drop_rate=0.0,
        drop_path_rate=0.1,
        drop_block_rate=None,
        img_size=224,
    )
    checkpoint_model = model.state_dict()
    for k in ['head.weight', 'head.bias', 'head_dist.weight', 'head_dist.bias']:
        if k in checkpoint_model :
            print(f"Removing key {k} from pretrained checkpoint")
            del checkpoint_model[k]
    pos_embed_checkpoint = checkpoint_model['pos_embed']
    embedding_size = pos_embed_checkpoint.shape[-1]
    num_patches = model.patch_embed.num_patches
    num_extra_tokens = model.pos_embed.shape[-2] - num_patches
    orig_size = int((pos_embed_checkpoint.shape[-2] - num_extra_tokens) ** 0.5)
    new_size = int(num_patches ** 0.5)
    extra_tokens = pos_embed_checkpoint[:, :num_extra_tokens]
    pos_tokens = pos_embed_checkpoint[:, num_extra_tokens:]
    pos_tokens = pos_tokens.reshape(-1, orig_size, orig_size, embedding_size).permute(0, 3, 1, 2)
    pos_tokens = torch.nn.functional.interpolate(
        pos_tokens, size=(new_size, new_size), mode='bicubic', align_corners=False)
    pos_tokens = pos_tokens.permute(0, 2, 3, 1).flatten(1, 2)
    new_pos_embed = torch.cat((extra_tokens, pos_tokens), dim=1)
    checkpoint_model['pos_embed'] = new_pos_embed
    del model.head
    model.load_state_dict(checkpoint_model, strict=False)
    model = VisionMambaBBox(base_model = model, num_classes=num_classes)
    model.to(DEVICE)
    model.train()
    return model

def load_model(name, reload_data=False, eval_size=10, batch_size=1, classes=['Car'], shuffle=True):
    if name == "yolov5":
        model = torch.hub.load('ultralytics/yolov5', 'yolov5s', pretrained=True)
        dataset = OpenImagesDatasetYolo('dataset', classes, download=reload_data, limit=eval_size)
        dataloader = DataLoader(dataset, batch_size=1, shuffle=False)
    elif name == "detr":
        model = torch.hub.load("facebookresearch/detr", "detr_resnet50", pretrained=True)
        model.eval()
        model.to(DEVICE)
        dataset = OpenImagesDataset('dataset', classes, download=reload_data, limit=eval_size)
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    elif name == 'mamba_train':
        dataset = OpenImagesDataset('dataset', classes, download=reload_data, limit=eval_size)
        num_classes = len(classes)
        model = load_mamba_model(num_classes)
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)
    elif name == "mamba_eval":
        pass
    return model, dataloader

if __name__ == "__main__":
    model_name = "mamba_train"
    eval_size = 500
    classes = ["Car", "Ambulance", "Bicycle", "Bus", "Helicopter", "Motorcycle", "Truck", "Van"]
    print("Using model: ", model_name)
    model, dataloader = load_model(model_name, reload_data=True, eval_size=eval_size, batch_size=1, classes=classes)
    if model_name == "yolov5":
        test_yolo(model, dataloader, save_predicted_img=True)
    elif model_name == "detr":
        test_vit(model, dataloader, save_predicted_img=True)
    elif model_name == "mamba_train":
        train_mamba(model, dataloader)
