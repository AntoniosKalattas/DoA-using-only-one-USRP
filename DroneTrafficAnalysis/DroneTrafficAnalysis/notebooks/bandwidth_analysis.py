#!/usr/bin/env python3
"""
Bandwidth Analysis and Visualization for Mavic 2 Drone States
Focuses on bandwidth_3db_hz as the key differentiating feature
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

# Set style
sns.set_style("whitegrid")
sns.set_palette("husl")

# Paths
DATA_DIR = Path(__file__).parent.parent / "dataset" / "data" / "processed"
FIG_DIR = Path(__file__).parent / "figures"
FIG_DIR.mkdir(exist_ok=True)

def load_data():
    """Load the full dataset"""
    df = pd.read_csv(DATA_DIR / "full_dataset.csv")
    print(f"Loaded {len(df)} samples")
    print(f"States: {df['state'].value_counts().to_dict()}")
    return df

def plot_bandwidth_boxplot(df):
    """Box plot of bandwidth by state"""
    fig, ax = plt.subplots(figsize=(10, 6))
    
    order = ['idle', 'hover', 'fly_fast']
    palette = {'idle': '#3498db', 'hover': '#f39c12', 'fly_fast': '#e74c3c'}
    
    sns.boxplot(data=df, x='state', y='bandwidth_3db_hz', order=order,
                palette=palette, ax=ax)
    
    ax.set_xlabel('Drone State', fontsize=12)
    ax.set_ylabel('Bandwidth at -3dB (Hz)', fontsize=12)
    ax.set_title('Bandwidth Distribution by Drone State\n(Mavic 2 Simulation Mode)', fontsize=14)
    
    # Add mean values as text
    for i, state in enumerate(order):
        mean_val = df[df['state'] == state]['bandwidth_3db_hz'].mean()
        ax.annotate(f'μ={mean_val/1000:.1f}kHz', xy=(i, mean_val), 
                   xytext=(i+0.25, mean_val), fontsize=10, color='black',
                   fontweight='bold')
    
    plt.tight_layout()
    plt.savefig(FIG_DIR / 'bandwidth_boxplot.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'bandwidth_boxplot.png'}")
    plt.close()

def plot_bandwidth_violin(df):
    """Violin plot showing bandwidth distribution"""
    fig, ax = plt.subplots(figsize=(10, 6))
    
    order = ['idle', 'hover', 'fly_fast']
    palette = {'idle': '#3498db', 'hover': '#f39c12', 'fly_fast': '#e74c3c'}
    
    sns.violinplot(data=df, x='state', y='bandwidth_3db_hz', order=order,
                   palette=palette, ax=ax, inner='quartile')
    
    ax.set_xlabel('Drone State', fontsize=12)
    ax.set_ylabel('Bandwidth at -3dB (Hz)', fontsize=12)
    ax.set_title('Bandwidth Distribution Density by State\n(Mavic 2 Simulation Mode)', fontsize=14)
    
    plt.tight_layout()
    plt.savefig(FIG_DIR / 'bandwidth_violin.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'bandwidth_violin.png'}")
    plt.close()

def plot_bandwidth_means(df):
    """Bar chart of mean bandwidth with error bars"""
    fig, ax = plt.subplots(figsize=(10, 6))
    
    order = ['idle', 'hover', 'fly_fast']
    colors = ['#3498db', '#f39c12', '#e74c3c']
    
    means = []
    stds = []
    for state in order:
        state_data = df[df['state'] == state]['bandwidth_3db_hz']
        means.append(state_data.mean())
        stds.append(state_data.std())
    
    x = np.arange(len(order))
    bars = ax.bar(x, means, yerr=stds, capsize=8, color=colors, alpha=0.7, 
                  edgecolor='black', linewidth=1.5)
    
    ax.set_xticks(x)
    ax.set_xticklabels(order, fontsize=11)
    ax.set_xlabel('Drone State', fontsize=12)
    ax.set_ylabel('Mean Bandwidth at -3dB (Hz)', fontsize=12)
    ax.set_title('Mean Bandwidth by Drone State\n(Error bars = 1 std dev)', fontsize=14)
    
    # Add value labels on bars
    for i, (mean, std) in enumerate(zip(means, stds)):
        ax.annotate(f'{mean/1000:.1f} kHz', xy=(i, mean + std + 10000), 
                   ha='center', fontsize=12, fontweight='bold')
    
    plt.tight_layout()
    plt.savefig(FIG_DIR / 'bandwidth_means.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'bandwidth_means.png'}")
    plt.close()

def plot_feature_comparison(df):
    """Compare all features across states - showing they're similar"""
    features = ['mean_power_db', 'std_power_db', 'max_power_db', 'min_power_db',
                'spectral_entropy', 'spectral_flatness', 'power_variance_rate', 
                'bandwidth_3db_hz']
    
    fig, axes = plt.subplots(2, 4, figsize=(16, 8))
    axes = axes.flatten()
    
    order = ['idle', 'hover', 'fly_fast']
    palette = {'idle': '#3498db', 'hover': '#f39c12', 'fly_fast': '#e74c3c'}
    
    for i, feature in enumerate(features):
        ax = axes[i]
        sns.boxplot(data=df, x='state', y=feature, order=order, 
                   palette=palette, ax=ax)
        ax.set_xlabel('')
        ax.set_ylabel('')
        
        # Highlight if bandwidth (the differentiator)
        if feature == 'bandwidth_3db_hz':
            ax.set_title('bandwidth_3db_hz ★', fontsize=11, fontweight='bold', color='green')
        else:
            ax.set_title(feature, fontsize=10, color='gray')
    
    fig.suptitle('Feature Comparison Across Drone States\n(Simulation Mode: Most features overlap significantly, bandwidth shows slight trend)', 
                fontsize=13, y=1.02)
    
    plt.tight_layout()
    plt.savefig(FIG_DIR / 'feature_comparison.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'feature_comparison.png'}")
    plt.close()

def plot_confusion_matrix():
    """Plot confusion matrix from saved model results"""
    import pickle
    import json
    
    model_dir = Path(__file__).parent.parent / "ml" / "models"
    
    # Load components
    with open(model_dir / "random_forest_model.pkl", 'rb') as f:
        model = pickle.load(f)
    with open(model_dir / "scaler.pkl", 'rb') as f:
        scaler = pickle.load(f)
    with open(model_dir / "label_encoder.pkl", 'rb') as f:
        label_encoder = pickle.load(f)
    with open(model_dir / "feature_columns.json", 'r') as f:
        feature_cols = json.load(f)
    
    # Load test data
    test = pd.read_csv(DATA_DIR / "test.csv")
    X_test = scaler.transform(test[feature_cols].values)
    y_test = label_encoder.transform(test['state'])
    y_pred = model.predict(X_test)
    
    # Confusion matrix
    from sklearn.metrics import confusion_matrix, accuracy_score
    cm = confusion_matrix(y_test, y_pred)
    accuracy = accuracy_score(y_test, y_pred)
    
    fig, ax = plt.subplots(figsize=(8, 6))
    
    classes = label_encoder.classes_
    sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', 
                xticklabels=classes, yticklabels=classes, ax=ax,
                annot_kws={'size': 16}, cbar_kws={'shrink': 0.8})
    
    ax.set_xlabel('Predicted State', fontsize=12)
    ax.set_ylabel('Actual State', fontsize=12)
    ax.set_title(f'Random Forest Confusion Matrix\nAccuracy: {accuracy:.1%} (≈ random guessing due to simulation mode)', 
                fontsize=13)
    
    plt.tight_layout()
    plt.savefig(FIG_DIR / 'confusion_matrix.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'confusion_matrix.png'}")
    plt.close()

def plot_feature_importance():
    """Plot feature importance from Random Forest"""
    import pickle
    import json
    
    model_dir = Path(__file__).parent.parent / "ml" / "models"
    
    with open(model_dir / "random_forest_model.pkl", 'rb') as f:
        model = pickle.load(f)
    with open(model_dir / "feature_columns.json", 'r') as f:
        feature_cols = json.load(f)
    
    # Get importance
    importance = model.feature_importances_
    indices = np.argsort(importance)[::-1]
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    colors = ['#e74c3c' if feature_cols[i] == 'bandwidth_3db_hz' else '#3498db' 
              for i in indices]
    
    y_pos = range(len(indices))
    ax.barh(y_pos, importance[indices], color=colors, alpha=0.8, edgecolor='black')
    ax.set_yticks(y_pos)
    ax.set_yticklabels([feature_cols[i] for i in indices])
    ax.invert_yaxis()
    ax.set_xlabel('Feature Importance', fontsize=12)
    ax.set_title('Random Forest Feature Importance\n(bandwidth_3db_hz highlighted in red)', fontsize=13)
    
    plt.tight_layout()
    plt.savefig(FIG_DIR / 'feature_importance.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'feature_importance.png'}")
    plt.close()

def plot_summary_report(df):
    """Create a comprehensive summary plot"""
    fig = plt.figure(figsize=(14, 10))
    
    # Create grid
    gs = fig.add_gridspec(2, 2, hspace=0.3, wspace=0.3)
    
    order = ['idle', 'hover', 'fly_fast']
    palette = {'idle': '#3498db', 'hover': '#f39c12', 'fly_fast': '#e74c3c'}
    
    # Plot 1: Bandwidth by state
    ax1 = fig.add_subplot(gs[0, 0])
    sns.boxplot(data=df, x='state', y='bandwidth_3db_hz', order=order,
                palette=palette, ax=ax1)
    ax1.set_title('Bandwidth Distribution', fontsize=12, fontweight='bold')
    ax1.set_xlabel('State')
    ax1.set_ylabel('Bandwidth (Hz)')
    
    # Plot 2: Mean power by state  
    ax2 = fig.add_subplot(gs[0, 1])
    sns.boxplot(data=df, x='state', y='mean_power_db', order=order,
                palette=palette, ax=ax2)
    ax2.set_title('Mean Power Distribution', fontsize=12, fontweight='bold')
    ax2.set_xlabel('State')
    ax2.set_ylabel('Power (dB)')
    
    # Plot 3: Bandwidth trend line
    ax3 = fig.add_subplot(gs[1, 0])
    means = [df[df['state']==s]['bandwidth_3db_hz'].mean()/1000 for s in order]
    stds = [df[df['state']==s]['bandwidth_3db_hz'].std()/1000 for s in order]
    colors_list = [palette[s] for s in order]
    
    ax3.bar(order, means, yerr=stds, capsize=8, color=colors_list, alpha=0.7, edgecolor='black')
    ax3.plot(order, means, 'ko-', markersize=10, linewidth=2)
    ax3.set_ylabel('Bandwidth (kHz)')
    ax3.set_title('Bandwidth Trend: idle → hover → fly_fast', fontsize=12, fontweight='bold')
    for i, (m, s) in enumerate(zip(means, stds)):
        ax3.annotate(f'{m:.1f}±{s:.1f}', (i, m+s+5), ha='center', fontsize=10)
    
    # Plot 4: Text summary
    ax4 = fig.add_subplot(gs[1, 1])
    ax4.axis('off')
    
    summary_text = """
    RESEARCH SUMMARY
    ================
    
    Dataset: Mavic 2 (Simulation Mode)
    Total Samples: 1,200 (400 per state)
    
    Key Finding:
    • Bandwidth shows increasing trend:
      idle (43.5kHz) → hover (48.8kHz) → fly_fast (54.1kHz)
    
    • Other features (power, entropy, etc.) 
      show NO significant difference between states
    
    ML Classification Results:
    • Random Forest: 33.3% accuracy
    • XGBoost: 31.7% accuracy
    • (Random guessing = 33.3%)
    
    CONCLUSION:
    Simulation mode does not produce distinct
    RF signatures for reliable classification.
    Real flight data needed for meaningful results.
    """
    ax4.text(0.1, 0.95, summary_text, transform=ax4.transAxes, fontsize=11,
             verticalalignment='top', fontfamily='monospace',
             bbox=dict(boxstyle='round', facecolor='#f0f0f0', alpha=0.8))
    
    fig.suptitle('DJI Mavic 2 Drone State RF Analysis\n(5.8 GHz OcuSync 2.0)', 
                fontsize=15, fontweight='bold', y=0.98)
    
    plt.savefig(FIG_DIR / 'summary_report.png', dpi=150, bbox_inches='tight')
    print(f"Saved: {FIG_DIR / 'summary_report.png'}")
    plt.close()

def main():
    print("=" * 60)
    print("MAVIC 2 DRONE STATE ANALYSIS - BANDWIDTH FOCUS")
    print("=" * 60)
    
    # Load data
    df = load_data()
    
    # Generate all plots
    print("\nGenerating visualizations...")
    plot_bandwidth_boxplot(df)
    plot_bandwidth_violin(df)
    plot_bandwidth_means(df)
    plot_feature_comparison(df)
    plot_confusion_matrix()
    plot_feature_importance()
    plot_summary_report(df)
    
    print("\n" + "=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)
    print(f"\nAll figures saved to: {FIG_DIR}")
    
    # Print summary
    print("\n--- KEY FINDINGS ---")
    for state in ['idle', 'hover', 'fly_fast']:
        bw = df[df['state'] == state]['bandwidth_3db_hz']
        pwr = df[df['state'] == state]['mean_power_db']
        print(f"{state:10}: bandwidth = {bw.mean()/1000:6.1f} kHz (±{bw.std()/1000:.1f}), power = {pwr.mean():.1f} dB")
    
    print("\n--- CONCLUSION ---")
    print("In simulation mode, RF features are highly similar across states.")
    print("Bandwidth shows a slight increasing trend (idle→hover→fly_fast)")
    print("but the overlap is too large for reliable ML classification (~33% accuracy).")
    print("Real flight data would likely show more distinct RF signatures.")

if __name__ == "__main__":
    main()
