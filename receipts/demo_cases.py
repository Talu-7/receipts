"""Bundled, fully offline demo cases.

IMPORTANT: these are *synthetic* examples built to exercise the three outcomes. They are not
real papers and no real authors are implicated. Receipts labels them as synthetic everywhere
(UI, receipt JSON, receipt HTML). Run real papers by pasting an arXiv link.
"""
from __future__ import annotations

LEAKAGE_SCRIPT = '''\
"""Reproduction of: "20 selected features give 94.0% 5-fold CV accuracy" (synthetic example).

Protocol as described in the paper's method section: feature selection (SelectKBest, k=20) is
fit on the training folds only. We also compute the variant in which selection is fit on the
full dataset before cross-validation, so any mismatch with the claim can be diagnosed.
"""
import json, os
import numpy as np
from sklearn.datasets import make_classification
from sklearn.feature_selection import SelectKBest, f_classif
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline

seed = int(os.environ.get("SEED", "0"))
X, y = make_classification(n_samples=300, n_features=2000, n_informative=10, n_redundant=0,
                           n_clusters_per_class=2, class_sep=1.15, flip_y=0.02, random_state=7)
cv = StratifiedKFold(5, shuffle=True, random_state=seed)

# (a) as described: selection inside each training fold
pipe = make_pipeline(SelectKBest(f_classif, k=20), LogisticRegression(max_iter=2000))
described = float(np.mean(cross_val_score(pipe, X, y, cv=cv))) * 100
print(f"seed={seed} selection-inside-folds accuracy = {described:.2f}")

# (b) variant: selection fit on ALL data first (test folds leak into feature selection)
Xs = SelectKBest(f_classif, k=20).fit_transform(X, y)
leaky = float(np.mean(cross_val_score(LogisticRegression(max_iter=2000), Xs, y, cv=cv))) * 100
print(f"seed={seed} selection-on-all-data accuracy    = {leaky:.2f}")

print("RECEIPTS_RESULT " + json.dumps({
    "metric": "5-fold CV accuracy (%)",
    "value": round(described, 3),
    "extras": {"variant_select_on_all_data": round(leaky, 3)},
}))
'''

CLEAN_SCRIPT = '''\
"""Reproduction of: "Standardised logistic regression reaches 96.9% 5-fold CV accuracy on
handwritten digits" (synthetic example, scikit-learn digits)."""
import json, os
import numpy as np
from sklearn.datasets import load_digits
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

seed = int(os.environ.get("SEED", "0"))
X, y = load_digits(return_X_y=True)
cv = StratifiedKFold(5, shuffle=True, random_state=seed)
pipe = make_pipeline(StandardScaler(), LogisticRegression(max_iter=5000))
acc = float(np.mean(cross_val_score(pipe, X, y, cv=cv))) * 100
print(f"seed={seed} 5-fold CV accuracy = {acc:.2f}")
print("RECEIPTS_RESULT " + json.dumps({"metric": "5-fold CV accuracy (%)", "value": round(acc, 3), "extras": {}}))
'''

BROKEN_SCRIPT = '''\
"""Reproduction of: "Clinical-notes classifier reaches 91.3% macro-F1" (synthetic example).
The paper's dataset is proprietary and not distributed with the code."""
import json, os
seed = int(os.environ.get("SEED", "0"))
path = os.environ.get("PAPER_DATASET_PATH", "/data/proprietary/clinical_notes_v3.parquet")
print(f"seed={seed} loading {path}")
with open(path, "rb") as f:   # raises FileNotFoundError: the data is not public
    data = f.read()
print("RECEIPTS_RESULT " + json.dumps({"metric": "macro-F1 (%)", "value": 0.0, "extras": {}}))
'''

TESTTUNE_SCRIPT = '''\
"""Reproduction of: "k-NN with a tuned neighbourhood size reaches 75.6% test accuracy" (synthetic example).

Protocol as described: k is chosen by 5-fold CV on the training data only. We also compute the variant
in which k is chosen to maximise accuracy on the TEST split itself.
"""
import json, os
import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.neighbors import KNeighborsClassifier

seed = int(os.environ.get("SEED", "0"))
X, y = make_classification(n_samples=250, n_features=20, n_informative=5, n_redundant=0, flip_y=0.12, class_sep=0.7, random_state=3)
ks = list(range(1, 60, 2))
described, tuned_on_test = [], []
for i in range(15):
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.15, random_state=seed * 100 + i, stratify=y)
    gs = GridSearchCV(KNeighborsClassifier(), {"n_neighbors": ks}, cv=5).fit(Xtr, ytr)   # described: choose k by CV on training data
    described.append(gs.score(Xte, yte))
    tuned_on_test.append(max(KNeighborsClassifier(k).fit(Xtr, ytr).score(Xte, yte) for k in ks))  # variant: choose k on the TEST set
v = float(np.mean(described)) * 100
t = float(np.mean(tuned_on_test)) * 100
print(f"seed={seed} k chosen by train-CV = {v:.2f}  k chosen on test = {t:.2f}")
print("RECEIPTS_RESULT " + json.dumps({"metric": "test accuracy (%)", "value": round(v, 3),
                                       "extras": {"variant_hyperparams_tuned_on_test_set": round(t, 3)}}))
'''

TRAINMETRIC_SCRIPT = '''\
"""Reproduction of: "A random forest reaches 100% accuracy" (synthetic example).

Protocol as described: accuracy on held-out data (5-fold CV). We also compute the accuracy measured on
the training data itself.
"""
import json, os
import numpy as np
from sklearn.datasets import make_classification
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score

seed = int(os.environ.get("SEED", "0"))
X, y = make_classification(n_samples=600, n_features=20, n_informative=5, n_redundant=0, flip_y=0.15, class_sep=0.7, random_state=11)
rf = RandomForestClassifier(n_estimators=100, random_state=seed)
cv = StratifiedKFold(5, shuffle=True, random_state=seed)
held_out = float(np.mean(cross_val_score(rf, X, y, cv=cv))) * 100      # described: held-out accuracy
train_acc = float(rf.fit(X, y).score(X, y)) * 100                       # variant: accuracy on the training data
print(f"seed={seed} held-out = {held_out:.2f}  training-set = {train_acc:.2f}")
print("RECEIPTS_RESULT " + json.dumps({"metric": "test accuracy (%)", "value": round(held_out, 3),
                                       "extras": {"variant_train_set_accuracy": round(train_acc, 3)}}))
'''

DUPES_SCRIPT = '''\
"""Reproduction of: "1-NN reaches 91.3% 5-fold CV accuracy" (synthetic example).

The released dataset contains exact duplicate rows. Protocol as described: duplicates are removed before
splitting. We also compute the accuracy when they are not removed (copies land in train and test).
"""
import json, os
import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.neighbors import KNeighborsClassifier

seed = int(os.environ.get("SEED", "0"))
X, y = make_classification(n_samples=500, n_features=15, n_informative=6, n_redundant=0, flip_y=0.1, class_sep=0.8, random_state=5)
dup = np.random.RandomState(1).choice(len(X), 350, replace=False)
Xd, yd = np.vstack([X, X[dup]]), np.concatenate([y, y[dup]])
knn = KNeighborsClassifier(1)
cv = StratifiedKFold(5, shuffle=True, random_state=seed)
Xu, idx = np.unique(Xd, axis=0, return_index=True)                      # described: duplicates removed first
described = float(np.mean(cross_val_score(knn, Xu, yd[idx], cv=cv))) * 100
no_dedup = float(np.mean(cross_val_score(knn, Xd, yd, cv=cv))) * 100    # variant: duplicates left in
print(f"seed={seed} dedup = {described:.2f}  no-dedup = {no_dedup:.2f}")
print("RECEIPTS_RESULT " + json.dumps({"metric": "5-fold CV accuracy (%)", "value": round(described, 3),
                                       "extras": {"variant_no_dedup": round(no_dedup, 3)}}))
'''

SVM_SCRIPT = '''\
"""Reproduction of: "A standardised RBF-SVM reaches 98.1% 5-fold CV accuracy on handwritten digits"
(synthetic example, scikit-learn digits)."""
import json, os
import numpy as np
from sklearn.datasets import load_digits
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

seed = int(os.environ.get("SEED", "0"))
X, y = load_digits(return_X_y=True)
cv = StratifiedKFold(5, shuffle=True, random_state=seed)
acc = float(np.mean(cross_val_score(make_pipeline(StandardScaler(), SVC(C=10, gamma=0.001)), X, y, cv=cv))) * 100
print(f"seed={seed} 5-fold CV accuracy = {acc:.2f}")
print("RECEIPTS_RESULT " + json.dumps({"metric": "5-fold CV accuracy (%)", "value": round(acc, 3), "extras": {}}))
'''

CASES: dict[str, dict] = {
    "leakage": {
        "id": "demo:leakage",
        "title": "Sparse-signal classification: 20 selected features reach 94.0% CV accuracy",
        "blurb": "Synthetic example of a classic evaluation bug: feature selection fit before cross-validation.",
        "expected": "discrepancy_found",
        "claim": {"id": "c1", "statement": "Selecting 20 features then training logistic regression reaches 94.0% 5-fold CV accuracy.",
                  "metric": "5-fold CV accuracy (%)", "value": 94.0, "unit": "%", "dataset": "synthetic 300x2000 (make_classification)",
                  "higher_is_better": True, "where": "Table 2"},
        "requirements": ["numpy", "scikit-learn"],
        "script": LEAKAGE_SCRIPT,
    },
    "reproduces": {
        "id": "demo:reproduces",
        "title": "Standardised logistic regression on handwritten digits: 96.9% CV accuracy",
        "blurb": "Synthetic example that reproduces cleanly, to show the agent does not flag everything.",
        "expected": "reproduced",
        "claim": {"id": "c1", "statement": "Standardised logistic regression reaches 96.9% 5-fold CV accuracy on digits.",
                  "metric": "5-fold CV accuracy (%)", "value": 96.9, "unit": "%", "dataset": "scikit-learn digits",
                  "higher_is_better": True, "where": "Table 1"},
        "requirements": ["numpy", "scikit-learn"],
        "script": CLEAN_SCRIPT,
    },
    "broken": {
        "id": "demo:broken",
        "title": "Clinical-notes classifier: 91.3% macro-F1 (proprietary data)",
        "blurb": "Synthetic example where the data is not public: the agent must say 'inconclusive', not accuse.",
        "expected": "inconclusive",
        "claim": {"id": "c1", "statement": "Classifier reaches 91.3% macro-F1 on the clinical-notes test set.",
                  "metric": "macro-F1 (%)", "value": 91.3, "unit": "%", "dataset": "proprietary clinical notes",
                  "higher_is_better": True, "where": "Table 3"},
        "requirements": [],
        "script": BROKEN_SCRIPT,
    },
    "testtune": {
        "id": "demo:testtune",
        "title": "k-NN with a tuned neighbourhood size: 75.6% test accuracy",
        "blurb": "Synthetic planted error: a hyperparameter chosen on the test set.",
        "expected": "discrepancy_found",
        "claim": {"id": "c1", "statement": "k-NN with a tuned k reaches 75.6% test accuracy.", "metric": "test accuracy (%)",
                  "value": 75.6, "unit": "%", "dataset": "synthetic 250x20 (make_classification)", "higher_is_better": True, "where": "Table 1"},
        "requirements": ["numpy", "scikit-learn"],
        "script": TESTTUNE_SCRIPT,
    },
    "trainmetric": {
        "id": "demo:trainmetric",
        "title": "Random forest reaches 100% accuracy",
        "blurb": "Synthetic planted error: accuracy measured on the training data, reported as test accuracy.",
        "expected": "discrepancy_found",
        "claim": {"id": "c1", "statement": "A random forest reaches 100% test accuracy on a noisy 600-sample task.", "metric": "test accuracy (%)",
                  "value": 100.0, "unit": "%", "dataset": "synthetic 600x20 with 15% label noise", "higher_is_better": True, "where": "Table 2"},
        "requirements": ["numpy", "scikit-learn"],
        "script": TRAINMETRIC_SCRIPT,
    },
    "dupes": {
        "id": "demo:dupes",
        "title": "1-NN reaches 91.3% CV accuracy",
        "blurb": "Synthetic planted error: duplicate rows leak across the train/test split.",
        "expected": "discrepancy_found",
        "claim": {"id": "c1", "statement": "1-NN reaches 91.3% 5-fold CV accuracy.", "metric": "5-fold CV accuracy (%)",
                  "value": 91.3, "unit": "%", "dataset": "synthetic 850 rows incl. 350 exact duplicates", "higher_is_better": True, "where": "Table 1"},
        "requirements": ["numpy", "scikit-learn"],
        "script": DUPES_SCRIPT,
    },
    "partial": {
        "id": "demo:partial",
        "title": "Logistic regression on digits: 98.4% CV accuracy (slightly overstated)",
        "blurb": "Synthetic mild overstatement: the true number is about 1.5 points lower. Expect 'partially reproduced'.",
        "expected": "partially_reproduced",
        "claim": {"id": "c1", "statement": "Standardised logistic regression reaches 98.4% 5-fold CV accuracy on digits.",
                  "metric": "5-fold CV accuracy (%)", "value": 98.4, "unit": "%", "dataset": "scikit-learn digits",
                  "higher_is_better": True, "where": "Table 1"},
        "requirements": ["numpy", "scikit-learn"],
        "script": CLEAN_SCRIPT,
    },
    "svm": {
        "id": "demo:svm",
        "title": "Standardised RBF-SVM on digits: 98.1% CV accuracy",
        "blurb": "Second clean synthetic example (negative control).",
        "expected": "reproduced",
        "claim": {"id": "c1", "statement": "A standardised RBF-SVM reaches 98.1% 5-fold CV accuracy on digits.",
                  "metric": "5-fold CV accuracy (%)", "value": 98.1, "unit": "%", "dataset": "scikit-learn digits",
                  "higher_is_better": True, "where": "Table 1"},
        "requirements": ["numpy", "scikit-learn"],
        "script": SVM_SCRIPT,
    },
}


def get_case(key: str) -> dict | None:
    key = key.strip().lower()
    if key.startswith("demo:"):
        key = key[5:]
    return CASES.get(key)
