"""
 Location: CLIP-FH/datasets/transforms.py
 What to Implement:
    - Image preprocessing and augmentations (resize, crop, normalize)
    - Compatible with CLIP (ImageNet normalization or CLIPs own)
"""

"""
transforms.py

Defines preprocessing and augmentation pipelines for hand images.
CLIP expects images resized to 224x224 and normalized with specific mean/std.
"""

from torchvision import transforms

def build_transforms(train=True):
    normalize = transforms.Normalize(
        mean=[0.48145466, 0.4578275, 0.40821073],
        std=[0.26862954, 0.26130258, 0.27577711]
    )

    if train:
        return transforms.Compose([
            #transforms.Resize((224, 224)),
            transforms.Resize((224, 128)),  # v11- preferred for hand ReID
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ])
    else:
        return transforms.Compose([
            #transforms.Resize((224, 224)),
            transforms.Resize((224, 128)),  # v11- referred for hand ReID
            transforms.ToTensor(),
            normalize,
        ])
