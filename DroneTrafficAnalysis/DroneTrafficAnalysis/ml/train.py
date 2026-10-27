#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ML Training Pipeline for DJI Drone Traffic Classification
Trains and evaluates multiple models on drone state classification

Usage:
    python train.py --data ../dataset/data/processed --model xgboost
"""

import pandas as pd
import numpy as np
import json
import pickle
import argparse
from pathlib import Path
from datetime import datetime

from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.metrics import (
    classification_report, confusion_matrix, accuracy_score,
    precision_score, recall_score, f1_score
)

# Try to import optional dependencies
try:
    import xgboost as xgb
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    print("XGBoost not available. Install with: pip install xgboost")

try:
    from sklearn.neural_network import MLPClassifier
    HAS_MLP = True
except ImportError:
    HAS_MLP = False


# Feature columns to use (exclude metadata columns)
EXCLUDE_COLS = ['timestamp', 'state', 'source_file', 'session', 'center_freq_mhz']


def load_data(data_dir):
    """Load train/val/test data from directory"""
    data_path = Path(data_dir)
    
    train = pd.read_csv(data_path / "train.csv")
    val = pd.read_csv(data_path / "val.csv")
    test = pd.read_csv(data_path / "test.csv")
    
    print(f"Loaded data from {data_dir}")
    print(f"  Train: {len(train)}, Val: {len(val)}, Test: {len(test)}")
    
    return train, val, test


def prepare_features(train, val, test):
    """Prepare features and labels"""
    # Get feature columns
    feature_cols = [c for c in train.columns if c not in EXCLUDE_COLS]
    print(f"Using {len(feature_cols)} features")
    
    # Encode labels
    label_encoder = LabelEncoder()
    y_train = label_encoder.fit_transform(train['state'])
    y_val = label_encoder.transform(val['state'])
    y_test = label_encoder.transform(test['state'])
    
    print(f"Classes: {list(label_encoder.classes_)}")
    
    # Extract features
    X_train = train[feature_cols].values
    X_val = val[feature_cols].values
    X_test = test[feature_cols].values
    
    # Scale features
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_val = scaler.transform(X_val)
    X_test = scaler.transform(X_test)
    
    return X_train, X_val, X_test, y_train, y_val, y_test, label_encoder, scaler, feature_cols


def get_model(model_name):
    """Get model by name"""
    models = {
        'random_forest': RandomForestClassifier(
            n_estimators=100,
            max_depth=20,
            min_samples_split=5,
            n_jobs=-1,
            random_state=42
        ),
        'gradient_boosting': GradientBoostingClassifier(
            n_estimators=100,
            max_depth=10,
            learning_rate=0.1,
            random_state=42
        ),
    }
    
    if HAS_XGBOOST:
        models['xgboost'] = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=10,
            learning_rate=0.1,
            n_jobs=-1,
            random_state=42,
            use_label_encoder=False,
            eval_metric='mlogloss'
        )
    
    if HAS_MLP:
        models['mlp'] = MLPClassifier(
            hidden_layer_sizes=(128, 64, 32),
            activation='relu',
            max_iter=500,
            early_stopping=True,
            random_state=42
        )
    
    if model_name not in models:
        print(f"Unknown model: {model_name}")
        print(f"Available models: {list(models.keys())}")
        return None
    
    return models[model_name]


def train_model(model, X_train, y_train, X_val, y_val):
    """Train a model"""
    print(f"\nTraining {model.__class__.__name__}...")
    
    start_time = datetime.now()
    model.fit(X_train, y_train)
    train_time = (datetime.now() - start_time).total_seconds()
    
    # Evaluate on validation
    y_pred = model.predict(X_val)
    val_accuracy = accuracy_score(y_val, y_pred)
    
    print(f"  Training time: {train_time:.2f}s")
    print(f"  Validation accuracy: {val_accuracy:.4f}")
    
    return model, train_time


def evaluate_model(model, X_test, y_test, label_encoder, output_dir=None):
    """Comprehensive model evaluation"""
    y_pred = model.predict(X_test)
    
    # Metrics
    accuracy = accuracy_score(y_test, y_pred)
    precision = precision_score(y_test, y_pred, average='weighted')
    recall = recall_score(y_test, y_pred, average='weighted')
    f1 = f1_score(y_test, y_pred, average='weighted')
    
    print("\n" + "="*60)
    print("TEST SET EVALUATION")
    print("="*60)
    print(f"Accuracy:  {accuracy:.4f}")
    print(f"Precision: {precision:.4f}")
    print(f"Recall:    {recall:.4f}")
    print(f"F1 Score:  {f1:.4f}")
    
    # Classification report
    print("\nClassification Report:")
    print(classification_report(
        y_test, y_pred, 
        target_names=label_encoder.classes_
    ))
    
    # Confusion matrix
    cm = confusion_matrix(y_test, y_pred)
    print("\nConfusion Matrix:")
    print(pd.DataFrame(
        cm, 
        index=label_encoder.classes_,
        columns=label_encoder.classes_
    ))
    
    results = {
        'accuracy': accuracy,
        'precision': precision,
        'recall': recall,
        'f1_score': f1,
        'confusion_matrix': cm.tolist(),
        'classes': list(label_encoder.classes_)
    }
    
    if output_dir:
        output_path = Path(output_dir)
        with open(output_path / "evaluation_results.json", 'w') as f:
            json.dump(results, f, indent=2)
    
    return results


def save_model(model, scaler, label_encoder, feature_cols, model_name, output_dir):
    """Save trained model and preprocessing artifacts"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Save model
    model_file = output_path / f"{model_name}_model.pkl"
    with open(model_file, 'wb') as f:
        pickle.dump(model, f)
    
    # Save scaler
    scaler_file = output_path / "scaler.pkl"
    with open(scaler_file, 'wb') as f:
        pickle.dump(scaler, f)
    
    # Save label encoder
    encoder_file = output_path / "label_encoder.pkl"
    with open(encoder_file, 'wb') as f:
        pickle.dump(label_encoder, f)
    
    # Save feature list
    with open(output_path / "feature_columns.json", 'w') as f:
        json.dump(feature_cols, f)
    
    print(f"\nModel saved to: {output_path}")
    print(f"  - {model_name}_model.pkl")
    print(f"  - scaler.pkl")
    print(f"  - label_encoder.pkl")
    print(f"  - feature_columns.json")


def get_feature_importance(model, feature_cols, top_n=20):
    """Get feature importance from tree-based models"""
    if hasattr(model, 'feature_importances_'):
        importances = model.feature_importances_
        indices = np.argsort(importances)[::-1][:top_n]
        
        print(f"\nTop {top_n} Important Features:")
        for i, idx in enumerate(indices):
            print(f"  {i+1:2d}. {feature_cols[idx]:40s} {importances[idx]:.4f}")
        
        return [(feature_cols[idx], importances[idx]) for idx in indices]
    return None


def main():
    parser = argparse.ArgumentParser(description='Train ML models for drone classification')
    parser.add_argument('--data', '-d', type=str, required=True,
                        help='Directory with train/val/test CSV files')
    parser.add_argument('--model', '-m', type=str, default='random_forest',
                        choices=['random_forest', 'gradient_boosting', 'xgboost', 'mlp'],
                        help='Model to train')
    parser.add_argument('--output', '-o', type=str, default='./models',
                        help='Output directory for trained model')
    args = parser.parse_args()
    
    # Load data
    train, val, test = load_data(args.data)
    
    # Prepare features
    X_train, X_val, X_test, y_train, y_val, y_test, label_encoder, scaler, feature_cols = \
        prepare_features(train, val, test)
    
    # Get model
    model = get_model(args.model)
    if model is None:
        return
    
    # Train
    model, train_time = train_model(model, X_train, y_train, X_val, y_val)
    
    # Evaluate
    results = evaluate_model(model, X_test, y_test, label_encoder, args.output)
    
    # Feature importance
    get_feature_importance(model, feature_cols)
    
    # Save model
    save_model(model, scaler, label_encoder, feature_cols, args.model, args.output)
    
    print("\n" + "="*60)
    print("TRAINING COMPLETE")
    print("="*60)


if __name__ == '__main__':
    main()
