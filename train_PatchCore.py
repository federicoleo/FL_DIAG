# 1 Pre - Trained ReNet50 Model
# 2 PatchCore Model
# 3 Train PatchCore Model
# 4 Evaluate PatchCore Model

import logging
import wandb
import argparse
from tqdm import tqdm

import numpy as np
import torch

import torchvision.models as models
import torch.nn.functional as F
from torchvision.models import ResNet50_Weights
from torch.utils.data import DataLoader, Subset

from DiversitySampling.src.coreset import CoresetSampler
from data.ksdd2 import KolektorSDD2
from data.mvtec import MVTEC
#from data.custom_dataset import CustomDataset

from torchvision import transforms
from PIL import ImageFilter
from torch import tensor
from sklearn.metrics import roc_auc_score

LOGGER = logging.getLogger(__name__)

class PatchCore(torch.nn.Module):
    def __init__(self):
        super(PatchCore, self).__init__()
        
        self.k_nearest = 3
        self.image_size = (224, 224)
        self.extracted_features = []
        self.memory_bank = []
        

        def hook(module, input, output): # module: layer, input: input to the layer, output: output of the layer
            self.extracted_features.append(output)
    
        self.model = models.resnet50(weights=ResNet50_Weights.IMAGENET1K_V1)

        self.model.layer2[-1].register_forward_hook(hook)
        self.model.layer3[-1].register_forward_hook(hook)

        # Disable gradient computation
        self.model.eval()
        for param in self.model.parameters():
            param.requires_grad = False

    def forward(self, sample):
            self.extracted_features = []
            _ = self.model(sample)

            return self.extracted_features


    def fit(self, dataloader : DataLoader):
        
        memory_items = []

        for sample, _ in tqdm(dataloader, total=len(dataloader)):
            # Extract features for the current batch
            self.extracted_features = []
            _ = self.model(sample)

            layer2_features = self.extracted_features[0] # Shape: [B, 512, H2, W2]
            layer3_features = self.extracted_features[1] # Shape: [B, 1024, H3, W3]

            layer2_processed = local_neighborhood_aggregation(layer2_features, p=3)
            layer3_processed = local_neighborhood_aggregation(layer3_features, p=3)

            # Upsample layer3 to match layer2's spatial dimensions
            layer3_upsampled = bilinear_upsample(layer3_processed, 
                                            target_size=layer2_processed.shape[2:])
            
            # Process each image in the batch
            B = layer2_processed.shape[0]
            for b in range(B):
                # Concatenate features from both layers along channel dimension
                combined = torch.cat([
                    layer2_processed[b],  # [512, H2, W2]
                    layer3_upsampled[b]   # [1024, H2, W2]
                ], dim=0)  # Result: [1536, H2, W2]
                
                # Resize to target dimension
                resized = bilinear_upsample(combined.unsqueeze(0), target_size=(28, 28)).squeeze(0)
                
                # Add to memory items collection
                memory_items.append(resized.unsqueeze(0))


        self.memory_bank = torch.cat(memory_items, dim=0)
        # [num_samples, 1536, 28, 28]
        
        N, C, H, W = self.memory_bank.shape
        print(f"Memory bank created with {N} samples of shape {C}x{H}x{W}")
        

        # Apply coreset sampling
        share = 0.01  # Keep 1% of all patches (adjust based on your needs)
        target_samples = max(1000, int(N * H * W * share))

        selected_embeddings, selected_indices = coreset_subsampling(
            embeddings=self.memory_bank,
            target_samples=target_samples,
            epsilon=0.1,
            device=self.device,
            use_projection=True
        )

        self.memory_bank = selected_embeddings

        print(f"Memory bank reduced from {N*H*W} to {len(selected_indices)} patch embeddings via coreset subsampling")

        # Save memory bank to disk
        # with open("memory_bank.pkl", "wb") as f:
        #     pickle.dump(self.memory_bank, f)

    def evaluate(self, test_dataloader: DataLoader):
        """
            Compute anomaly detection score and relative segmentation map for
            each test sample. Returns the ROC AUC computed from predictions scores.

            Returns:
            - image-level ROC-AUC score
            - pixel-level ROC-AUC score
        """

        image_preds = []
        image_labels = []
        pixel_preds = []
        pixel_labels = []

        for sample, mask, label in tqdm(test_dataloader):
            sample = sample.to(self.device)
            mask = mask.to(self.device)

            image_labels.append(label.numpy())
            pixel_labels.extend(mask.flatten().cpu().numpy())

            score, segm_map = self.predict(sample)  # Anomaly Detection

            image_preds.append(score.cpu().numpy())
            pixel_preds.extend(segm_map.flatten().cpu().numpy())

        image_labels = np.concatenate(image_labels)
        image_preds = np.array(image_preds)

        # Compute ROC AUC for prediction scores
        image_level_rocauc = roc_auc_score(image_labels, image_preds)
        pixel_level_rocauc = roc_auc_score(pixel_labels, pixel_preds)

        return image_level_rocauc, pixel_level_rocauc

    def predict(self, sample):
        # Patch Extraction
        
        # Get features through forward method
        feature_maps = self(sample)
        # Local Neighborhood Aggregation
        feature_maps = [local_neighborhood_aggregation(fm, p=3) for fm in feature_maps]
        # Matching Feature Dimensions
        feature_maps[1] = bilinear_upsample(feature_maps[1], target_size=feature_maps[0].shape[2:])
        # Concatenation and Resizing
        patch_collection = torch.cat(feature_maps, dim=1)
        patch_collection = patch_collection.reshape(patch_collection.shape[1], -1).T
        
        # Calculate distances
        distances = torch.cdist(patch_collection, self.memory_bank, p=2.0) # Shape: (784, N_subsampled×28×28)
        # Get the minimum distance for each patch
        dist_score, dist_score_idx = torch.min(distances, dim=1) # minimum distance for each patch
        s_idx = torch.argmax(dist_score) # index of the patch with the maximum distance
        s_star = torch.max(dist_score) # maximum distance, RAW ANOMALY SCORE
        
        m_test_star = patch_collection[s_idx] # feature vector of the most anomalous patch, actual embedding of the most anomalous patch
        m_star = self.memory_bank[dist_score_idx[s_idx]].unsqueeze(0) # embedding of the nearest neighbor of the most anomalous patch

        # KNN
        knn_dists = torch.cdist(m_star, self.memory_bank, p=2.0)        # L2 norm dist btw m_star with each patch of memory bank
        _, nn_idxs = knn_dists.topk(k=self.k_nearest, largest=False)    # Values and indexes of the k smallest elements of knn_dists

        # Compute image-level anomaly score s
        m_star_neighbourhood = self.memory_bank[nn_idxs[0, 1:]] # How far your anomalous patch is from each patch in the neighborhood
        w_denominator = torch.linalg.norm(m_test_star - m_star_neighbourhood, dim=1)    # Sum over the exp of l2 norm distances btw m_test_star and the m* neighbourhood
        norm = torch.sqrt(torch.tensor(patch_collection.shape[1]))                                 # Softmax normalization trick to prevent exp(norm) from becoming infinite
        w = 1 - (torch.exp(s_star / norm) / torch.sum(torch.exp(w_denominator / norm))) # Equation 7 from the paper
        s = w * s_star

        # Segmentation map
        fmap_size = feature_maps[0].shape[-2:]          # Feature map sizes: h, w
        segm_map = dist_score.view(1, 1, *fmap_size)    # Reshape distance scores tensor, (1, 1, h, w)
        segm_map = bilinear_upsample(segm_map, (self.image_size, self.image_size))  # Upsample to original image size
        segm_map = gaussian_blur(segm_map)              # Gaussian blur of kernel width = 4

        return s, segm_map


def local_neighborhood_aggregation(feature_map, p=3):
    """
    Performs local neighborhood aggregation by:
    1. Expanding each position to its pxp neighborhood
    2. Applying adaptive average pooling to each neighborhood
    3. Using the pooled result as the new feature at that position
    
    Args:
        feature_map (torch.Tensor): Input tensor of shape (B, C, H, W)
        p (int): Size of the neighborhood patch (default: 3 for 3x3)
    
    Returns:
        torch.Tensor: Reconstructed feature map with same shape as input
    """
    B, C, H, W = feature_map.shape
    offset = p // 2
    
    # Create output tensor
    reconstructed = torch.zeros_like(feature_map)
    
    # Pad input to handle border cases - this makes extraction easier
    padded = F.pad(feature_map, (offset, offset, offset, offset), mode='reflect')
    
    # Extract all neighborhoods at once using unfold
    # This gives us tensor of shape [B, C*p*p, H*W]
    neighborhoods = F.unfold(padded, kernel_size=p, stride=1)
    
    # Reshape to [B*H*W, C, p, p] to prepare for adaptive_avg_pool2d
    # First reshape to [B, C, p*p, H*W]
    neighborhoods = neighborhoods.view(B, C, p*p, H*W)
    # Then transpose and reshape to get [B*H*W, C, p, p]
    neighborhoods = neighborhoods.permute(0, 3, 1, 2).reshape(B*H*W, C, p, p)
    
    # Apply adaptive_avg_pool2d to get [B*H*W, C, 1, 1]
    pooled = F.adaptive_avg_pool2d(neighborhoods, (1, 1))
    
    # Reshape back to [B, H, W, C]
    pooled = pooled.reshape(B, H*W, C, 1).squeeze(-1).permute(0, 2, 1)
    
    # Finally reshape to [B, C, H, W]
    reconstructed = pooled.reshape(B, C, H, W)
    
    return reconstructed


def bilinear_upsample(lower_spatial_block, target_size):
    """
    Upsamples the given feature map to the target spatial size using bilinear interpolation.

    Args:
        lower_spatial_block (torch.Tensor): Input tensor of shape (B, C, H, W)
        target_size (tuple): Target spatial size (H_out, W_out)

    Returns:
        torch.Tensor: Upsampled feature map with shape (B, C, H_out, W_out)
    """
    if lower_spatial_block.shape[2:] == target_size:
        return lower_spatial_block
    else:
        return F.interpolate(lower_spatial_block, size=target_size, mode='bilinear', align_corners=False)


def random_projection(embeddings, target_dim, epsilon=0.1, seed=0):
    """
    Applies random projection to reduce embedding dimensionality using Johnson-Lindenstrauss lemma.
    
    Args:
        embeddings (torch.Tensor): Input embeddings of shape [N, C]
        target_dim (int): Target dimension to project to
        epsilon (float): Error tolerance for distance preservation
        seed (int): Random seed for reproducibility
        
    Returns:
        torch.Tensor: Projected embeddings of shape [N, target_dim]
    """
    # Get original dimensions
    N, C = embeddings.shape
    
    # Set random seed for reproducibility
    torch.manual_seed(seed)
    
    # Create random projection matrix
    projection_matrix = torch.randn(C, target_dim, device=embeddings.device)
    
    
    # Normalize columns to ensure distance preservation properties
    # Normalize the projection matrix (l2 norm with the sqrt)
    projection_matrix = projection_matrix / torch.sqrt(torch.sum(projection_matrix**2, dim=0, keepdim=True))
    
    # Apply projection
    # we multiply our patch embeddings with the projection matrix
    # relative distances between points are approximately preserved (that's the key benefit of Johnson-Lindenstrauss projection)
    projected = torch.matmul(embeddings, projection_matrix)
    
    return projected


def calculate_projection_dim(n_samples, original_dim, epsilon=0.1):
    """
    Calculate the minimum dimension needed for projection according to Johnson-Lindenstrauss lemma.
    
    Args:
        n_samples (int): Number of samples in the dataset
        original_dim (int): Original feature dimension
        epsilon (float): Desired error bound (typically 0.1-0.3)
        
    Returns:
        int: Target dimension for projection
    """
    # JL lemma formula for the minimum dimension
    # Calculate reduced dimension d* based on Johnson-Lindenstrauss lemma
    # The formula is derived from ensuring epsilon-distortion with high probability
    jl_dim = int(4 * np.log(n_samples) / (epsilon**2/2 - epsilon**3/3))
    
    # Can't project to higher dimension than original
    return min(original_dim, jl_dim)


def coreset_subsampling(embeddings, target_samples, epsilon=0.1, device=None, use_projection=True):
    """
    Applies coreset subsampling to select representative embeddings.
    
    Args:
        embeddings (torch.Tensor): Input embeddings of shape [N, C] or [N, C, H, W]
        target_samples (int): Number of samples to select
        epsilon (float): Error tolerance for random projection
        device (str): Device to use for computation
        use_projection (bool): Whether to apply random projection
        
    Returns:
        torch.Tensor: Selected embeddings
        list: Indices of selected embeddings
    """
    
    # Determine device
    if device is None:
        device = embeddings.device
    
    # Handle different input shapes
    original_shape = embeddings.shape
    if len(original_shape) > 2:
        # For feature maps [N, C, H, W], reshape to [N*H*W, C]
        N, C, H, W = embeddings.shape
        reshaped_embeddings = embeddings.permute(0, 2, 3, 1).reshape(-1, C)
    else:
        # Already in correct shape [N, C]
        reshaped_embeddings = embeddings
    
    n_samples, C = reshaped_embeddings.shape
    
    # Step 1: Apply random projection if requested and beneficial
    if use_projection and C > 10:  # Only project if dimension is substantial
        # Calculate target projection dimension
        # d_star = calculate_projection_dim(n_samples, C, epsilon)
        d_star = 128
        
        # Apply projection if it reduces dimension
        if d_star < C:
            print(f"Projecting from {C} to {d_star} dimensions")
            embeddings_for_sampling = random_projection(reshaped_embeddings, d_star, epsilon)
        else:
            print(f"Skipping projection as calculated dimension {d_star} ≥ original {C}")
            embeddings_for_sampling = reshaped_embeddings
    else:
        # Skip projection
        embeddings_for_sampling = reshaped_embeddings
    
    # Step 2: Apply coreset sampling
    # Ensure we don't try to select more samples than available
    target_samples = min(target_samples, n_samples)
    
    # Initialize and run sampler
    sampler = CoresetSampler(
        n_samples=target_samples,
        device=str(device),
        tqdm_disable=False,
        verbose=1
    )
    
    # Get indices of selected samples
    selected_indices = sampler.sample(embeddings_for_sampling.cpu().numpy())
    
    # Step 3: Return selected embeddings in original space
    selected_embeddings = reshaped_embeddings[selected_indices]
    
    print(f"Reduced from {n_samples} to {len(selected_indices)} samples")
    
    return selected_embeddings, selected_indices

def gaussian_blur(img: tensor) -> tensor:
    """
        Apply a gaussian smoothing with sigma = 4 over the input image.
    """
    # Setup
    blur_kernel = ImageFilter.GaussianBlur(radius=4)
    tensor_to_pil = transforms.ToPILImage()
    pil_to_tensor = transforms.ToTensor()

    # Smoothing
    max_value = img.max()   # Maximum value of all elements in the image tensor
    blurred_pil = tensor_to_pil(img[0] / max_value).filter(blur_kernel)
    blurred_map = pil_to_tensor(blurred_pil) * max_value

    return blurred_map

def main(args):
    # Set the device.
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Using device: {device}")

    # Set the seed for reproducibility.
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    model = PatchCore().to(device)
    
    # Create transformations with proper resizing and center cropping
    transform = transforms.Compose([
        transforms.Resize(256),  # Resize the smaller edge to 256
        transforms.CenterCrop(224),  # Center crop to 224x224
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    ])    

    add_augmented = args.add_augmented
    num_augmented = args.num_augmented
    negative_only = args.negative_only
    zero_shot = args.zero_shot
    logging = args.logging

    run_name = f'PatchCore-zero_shot_{zero_shot}-add_augmented_{add_augmented}-num_augmented_{num_augmented}-bs_{args.batch_size}-epochs_{args.epochs}'
    tags = [f'{args.epochs}epochs', f'{num_augmented}augmented']
    if args.zero_shot:
        tags.append('zero_shot')
    else:
        tags.append('full_shot')
    if args.add_augmented:
        tags.append('augmented')
    else:
        tags.append('not_augmented')
    
    if logging:
        # Start a new wandb run to track this script.
        wandb.init(
            name=run_name,
            config=args,
            tags=tags
        )

    # Dataset.
    # Set up dataset based on the specified dataset name
    if args.dataset == 'ksdd2':
        print('Loading KolektorSDD2 training set...')
        train_data = KolektorSDD2(dataroot=args.dataset_path, split='train',
                                  negative_only=negative_only, 
                                  add_augmented=add_augmented,
                                  num_augmented=num_augmented, 
                                  zero_shot=zero_shot)
        
        print('Loading KolektorSDD2 test set...')
        test_data = KolektorSDD2(dataroot=args.dataset_path, split='test')
    
    elif args.dataset == 'mvtec':
        print('Loading MVTec training set...')
        train_data = MVTEC(dataroot=args.dataset_path,
                           split='train',
                           negative_only=negative_only,
                           add_augmented=add_augmented,
                           num_augmented=num_augmented,
                           zero_shot=zero_shot)
        
        print('Loading MVTec test set...')
        test_data = MVTEC(dataroot=args.dataset_path,
                          split='test')
    else:
        raise ValueError(f"Unknown dataset: {args.dataset}")

    print(f'Training samples: {len(train_data)}')
    print(f'Testing samples: {len(test_data)}')

    # DataLoaders.
    train_loader = DataLoader(train_data, batch_size=args.batch_size,
                              shuffle=True, num_workers=args.num_workers, transform=transform)
    test_loader = DataLoader(test_data, batch_size=args.batch_size,
                             shuffle=False, num_workers=args.num_workers, transform=transform)


    # PatchCore doesn't use traditional training with backpropagation
    # Instead, it builds a memory bank from normal samples
    print(f'Building PatchCore memory bank on {device} [...]')
    
    # Extract features and build memory bank
    model.fit(train_loader)
    
    # Evaluate the model
    print('Evaluating PatchCore model...')
    image_auc, pixel_auc = model.evaluate(test_loader)
    
    print(f'Image-level AUC: {image_auc:.4f}')
    print(f'Pixel-level AUC: {pixel_auc:.4f}')
    
    if logging:
        wandb.log({
            'image_auc': image_auc,
            'pixel_auc': pixel_auc
        })
        wandb.finish()
    print('PatchCore evaluation finished.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DIAG training')
    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--epochs', type=int, default=30)
    parser.add_argument('--batch_size', type=int, default=32)
    parser.add_argument('--num_workers', type=int, default=8)
    parser.add_argument('--dataset', type=str, choices=['ksdd2', 'mvtec'], default='ksdd2', help='Dataset to use for training (ksdd2 or mvtec)')
    parser.add_argument('--dataset_path', type=str, required=True)
    parser.add_argument('--negative_only', action='store_true', help='Train the model with only negative samples')
    parser.add_argument('--add_augmented', action='store_true', help='Add augmented images to the training set')
    parser.add_argument('--num_augmented', type=int, default=120)
    parser.add_argument('--zero_shot', action='store_true', help='Train the model without true positives in the training set')
    parser.add_argument('--logging', action='store_true', help='Log the stats to wandb')
    

    args = parser.parse_args()
    main(args)


