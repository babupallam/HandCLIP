import os
import sys

#  Add the project root to sys.path for local imports to work
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import yaml
import torch
import clip
import time
from datetime import datetime
from utils.save_load_models import load_checkpoint

# Local imports from your repository
from engine.baseline_inference import extract_features, compute_similarity_matrix
from engine.evaluator import evaluate_rank
from utils.dataloaders import get_dataloader
from utils.naming import build_filename
from utils.train_helpers import register_bnneck_and_arcface
from engine.prompt_learner import PromptLearner

# --- PromptSG specific helpers -----------------------------
from utils.save_load_models import load_promptsg_checkpoint
from utils.train_helpers import build_promptsg_models, compose_prompt
from engine.promptsg_inference import extract_features_promptsg


def load_config(path):
    """
    Load configuration from a YAML file.

    Args:
        path (str): Path to the YAML configuration file.

    Returns:
        dict: A dictionary representing the parsed configuration.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:  #  Fix here
        return yaml.safe_load(f)


def setup_log_file(config, config_path):
    """
    Set up the log file where evaluation details will be stored.

    This function:
      1. Derives a name for the experiment from the config.
      2. Creates a directory (if needed) for logs.
      3. Creates and initializes a log file with basic info about the run.

    Args:
        config (dict): The evaluation configuration dictionary.
        config_path (str): The path to the YAML config for reference.

    Returns:
        str: The path to the newly created log file.
    """
    # Derive a name for this experiment (or use the default naming scheme)
    exp_name = config.get("experiment", f"eval_{config['variant']}_{config['model']}_{config['dataset']}_{config['aspect']}")

    # Directory where the log file will be saved
    log_dir = config.get("output_dir", "eval_logs/")
    os.makedirs(log_dir, exist_ok=True)

    log_filename = build_filename(config, epoches=config.get("epochs_image", 0), stage="image", extension=".log",
                                  timestamped=False)
    log_path = os.path.join(log_dir, log_filename)

    # Initialize the log file with basic run info
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("Evaluation Log\n")
        f.write("=" * 50 + "\n")
        f.write(f"Config      : {os.path.abspath(config_path)}\n")
        f.write(f"Experiment  : {exp_name}\n")
        f.write(f"Model       : {config['model']}\n")
        f.write(f"Variant     : {config.get('variant', 'baseline')}\n")
        f.write(f"Dataset     : {config['dataset']}\n")
        f.write(f"Aspect      : {config['aspect']}\n")
        f.write(f"Model Path  : {config.get('model_path', 'N/A')}\n")
        f.write("=" * 50 + "\n\n")

    return log_path


def log(log_path, text):
    """
    Helper function to log a given message both to console and to a file.

    Args:
        log_path (str): Path to the log file.
        text (str): The text message to log.
    """
    # Print to console so the user can see progress
    print(text)
    # Append the message to the log file
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def run_eval(config_path):
    # 1. Read the YAML configuration.
    config = load_config(config_path)

    # 2. Prepare the log file for the evaluation.
    log_path = setup_log_file(config, config_path)

    # Extract some essential info from config.
    model_name = config["model"]
    dataset = config["dataset"]
    aspect = config["aspect"]

    variant = config.get("variant", "baseline").lower()

    # allow autodetection when the YAML forgot to set variant: promptsg
    def _is_promptsg(metadata):
        return metadata.get("stage", "").lower() == "promptsg"

    is_promptsg = variant == "promptsg"


    model_path = config.get("model_path")
    batch_size = config.get("batch_size")
    num_splits = config.get("num_splits", 10)


    # Decide on device (GPU if available, otherwise CPU).
    device = "cuda" if torch.cuda.is_available() else "cpu"
    log(log_path, f"Device: {device}")

    # 3. Load a CLIP model from the specified model name.
    model_map = {
        "vitb16": "ViT-B/16",
        "vitb32": "ViT-B/32",
        "rn50": "RN50",
        "rn101": "RN101",
        "rn50x4": "RN50x4",
        "rn50x16": "RN50x16",
        "rn50x64": "RN50x64"
    }

    clip_name = model_map.get(model_name.lower())
    if clip_name is None:
        raise ValueError(f"Unknown model name: {model_name}. Must be one of: {list(model_map.keys())}")

    model, preprocess = clip.load(clip_name, device=device)

    if model_path:
        if not os.path.exists(model_path):
            log(log_path, f"[ERROR] Specified checkpoint does not exist: {model_path}")
            raise FileNotFoundError(f"Checkpoint not found at {model_path}")

        log(log_path, f"Loading fine-tuned model from: {model_path}")

        # ===== PROMPTSG CHECKPOINT LOADING (safe fallback) =====
        if is_promptsg:
            from utils.save_load_models import _torch_load_flexible
            ckpt_raw = _torch_load_flexible(model_path, map_location=device)

            # Load CLIP backbone only at this point
            model.load_state_dict(ckpt_raw["clip_state_dict"], strict=False)

            # Extract rest of info for later use
            epoch_ckpt = ckpt_raw.get("epoch", 0)
            config_ckpt = ckpt_raw.get("config", {})
            metadata_ckpt = ckpt_raw.get("metadata", {})
            checkpoint = ckpt_raw  # Optional: keep for classifier_state_dict

            log(log_path, "[PromptSG] PromptSG checkpoint loaded")
            log(log_path, f"Checkpoint restored from epoch {epoch_ckpt}")
            log(log_path, f"Checkpoint metadata: {metadata_ckpt}")

        # ===== NON-PROMPTSG (baseline / clipreid) =====
        else:
            checkpoint, config_ckpt, epoch_ckpt, metadata_ckpt = load_checkpoint(
                path=model_path,
                model=model,
                device=device,
                config=config
            )
            log(log_path, f"Model weights loaded successfully from: {model_path}")
            log(log_path, f"Checkpoint restored from epoch {epoch_ckpt}")
            log(log_path, f"Checkpoint metadata: {metadata_ckpt}")

        # ===== PromptSG module setup (AFTER we extract config) =====
        if is_promptsg or _is_promptsg(metadata_ckpt):
            log(log_path, "[PromptSG] Detected PromptSG checkpoint  rebuilding submodules")

            cfg_for_modules = config_ckpt if "pseudo_token_dim" in config_ckpt else config
            num_classes = checkpoint.get("classifier_state_dict", {}).get("weight", torch.empty(0)).size(0)

            inversion_model, multimodal_module, reduction, bnneck,  classifier = build_promptsg_models(
                cfg_for_modules, num_classes, device
            )

            # Now that the submodules exist, load them
            _, _, _ = load_promptsg_checkpoint(
                path=model_path,
                model_components=(model, inversion_model, multimodal_module, classifier),
                device=device
            )

            inversion_model.eval()
            multimodal_module.eval()
            classifier.eval()
            prompt_learner = None  # PromptSG does not use PromptLearner

        # ===== ClipReID PromptLearner setup =====
        elif variant == "clipreid":
            from torchvision.datasets import ImageFolder
            from utils.transforms import build_transforms

            if dataset == "11k":
                dataset_root = os.path.join("datasets", f"11khands/train_val_test_split_{aspect}")
            elif dataset.lower() == "hd":
                dataset_root = os.path.join("datasets", "HD/Original Images/train_val_test_split")
            else:
                raise ValueError(f"Unsupported dataset: {dataset}")

            train_dir = os.path.join(dataset_root, "train")
            transform = build_transforms(train=False)
            dataset_train = ImageFolder(train_dir, transform=transform)
            class_to_idx = dataset_train.class_to_idx
            classnames = [k for k, _ in sorted(class_to_idx.items(), key=lambda x: x[1])]

            prompt_learner = PromptLearner(
                classnames=classnames,
                clip_model=model,
                n_ctx=config.get("n_ctx", 16),
                ctx_init=config.get("ctx_init", None),
                prompt_template=config.get("prompt_template", "a photo of a {} hand."),
                aspect=config.get("aspect", "dorsal"),
                device=device
            )

            prompt_path = model_path.replace(".pth", "_prompt.pth")
            if os.path.exists(prompt_path):
                prompt_learner.load_state_dict(torch.load(prompt_path, map_location=device))
                prompt_learner.to(device)
                prompt_learner.eval()
                log(log_path, f"Prompt Learner weights loaded from: {prompt_path}")
            else:
                log(log_path, f"[WARNING] Expected prompt weights not found at: {prompt_path}")

            inversion_model = multimodal_module = classifier = None

        # ===== Baseline (no PromptSG, no PromptLearner) =====
        else:
            prompt_learner = None
            inversion_model = multimodal_module = classifier = None

    else:
        log(log_path, "No fine-tuned model path specified. Using official CLIP baseline.")
        prompt_learner = None
        inversion_model = multimodal_module = classifier = None




    # Make sure model is in eval mode for inference
    model.eval()

    # Construct the base path that contains all splits (query/gallery).
    # This can vary depending on dataset (11k vs HD).
    base_path = os.path.join("datasets",
                             "11khands" if dataset == "11k" else "HD/Original Images",
                             "train_val_test_split")


    # If using the 11k dataset, further append the aspect specification.
    if dataset == "11k":
        base_path = os.path.join(base_path + f"_{aspect}")

    print(f"dataset path: ", base_path)

    # Lists to accumulate rank-1 and mAP for each split.
    all_rank1, all_rank5, all_rank10, all_map = [],[],[],[]

    # 4. Evaluate on each split (query/gallery pairs).
    for i in range(num_splits):
        query_path = os.path.join(base_path, f"query{i}")
        gallery_path = os.path.join(base_path, f"gallery{i}")

        # If either folder doesnt exist, we skip.
        if not os.path.exists(query_path) or not os.path.exists(gallery_path):
            log(log_path, f" Skipping missing split {i}: {query_path} / {gallery_path}")
            continue

        log(log_path, f"\nSplit {i + 1}/{num_splits}")
        start_time = time.time()

        # Prepare full image folder paths
        query_path = os.path.join(base_path, f"query{i}")
        gallery_path = os.path.join(base_path, f"gallery{i}")

        # Build loaders from image folders
        query_loader = get_dataloader(query_path, batch_size=batch_size, shuffle=False, train=False)
        gallery_loader = get_dataloader(gallery_path, batch_size=batch_size, shuffle=False, train=False)

        # Extract features from loaders
        use_flip = config.get("use_flip", False)  #l horizontal flip feature averaging (like MBA)
        # This will mirror the MBA strategy, where features from original and horizontally flipped images are averaged, improving generalization for re-identification.
        # this feature can be used from the config file

        if inversion_model is not None:  #  PromptSG path
            q_feats, q_labels = extract_features_promptsg(
                model, inversion_model, multimodal_module, classifier,
                query_loader, device, compose_prompt
            )
            g_feats, g_labels = extract_features_promptsg(
                model, inversion_model, multimodal_module, classifier,
                gallery_loader, device, compose_prompt
            )
        else:  #  baseline / clipreid path
            q_feats, q_labels = extract_features(
                model, query_loader, device,
                use_flip=use_flip, prompt_learner=prompt_learner
            )
            g_feats, g_labels = extract_features(
                model, gallery_loader, device,
                use_flip=use_flip, prompt_learner=prompt_learner
            )

        # Compute similarity between query & gallery
        sim_matrix = compute_similarity_matrix(q_feats, g_feats)

        # Evaluate metrics (Rank-1, Rank-5, Rank-10, mAP)
        metrics = evaluate_rank(sim_matrix, q_labels, g_labels, topk=[1, 5, 10])

        # Convert to percentages
        metrics = {k: v * 100 for k, v in metrics.items()}

        # Track all for final average
        all_rank1.append(metrics.get("Rank-1", 0))
        all_rank5.append(metrics.get("Rank-5", 0))
        all_rank10.append(metrics.get("Rank-10", 0))
        all_map.append(metrics.get("mAP", 0))

        # Log all ranks and mAP
        log(log_path, "Evaluation Metrics:")
        log(log_path, f"   Rank-1 : {metrics.get('Rank-1', 0):.2f}%")
        log(log_path, f"   Rank-5 : {metrics.get('Rank-5', 0):.2f}%")
        log(log_path, f"   Rank-10: {metrics.get('Rank-10', 0):.2f}%")
        log(log_path, f"   mAP    : {metrics.get('mAP', 0):.2f}%")
        log(log_path, f"Time Taken     : {time.time() - start_time:.2f}s")

    # 5. Compute and log the averaged results if we evaluated at least one split.
    if all_rank1:
        avg_rank1 = sum(all_rank1) / len(all_rank1)
        avg_rank5 = sum(all_rank5) / len(all_rank5)
        avg_rank10 = sum(all_rank10) / len(all_rank10)
        avg_map = sum(all_map) / len(all_map)

        log(log_path, "\nFinal Averaged Results Across All Splits:")
        log(log_path, f"Rank-1 Accuracy : {avg_rank1:.2f}%")
        log(log_path, f"Rank-5 Accuracy : {avg_rank5:.2f}%")
        log(log_path, f"Rank-10 Accuracy: {avg_rank10:.2f}%")
        log(log_path, f"Mean AP         : {avg_map:.2f}%")
    else:
        log(log_path, "\nNo splits were evaluated. Check dataset path.")

    # 6. Indicate where the log was saved
    log(log_path, f"\nLog saved to: {log_path}")


if __name__ == "__main__":
    """
    Entry-point for command-line usage.

    Example usage:
        python run_eval_clip.py --config path/to/config.yml
    """
    # Expect either:
    #   python run_eval_clip.py --config config.yml
    # or
    #   python run_eval_clip.py config.yml
    if len(sys.argv) != 3 and len(sys.argv) != 2:
        print("Usage: python run_eval_clip.py --config path/to/config.yml")
        sys.exit(1)

    # The config file is the last argument in both usage patterns.
    run_eval(sys.argv[-1])
