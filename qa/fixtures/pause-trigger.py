import os


def collect_paths(root):
    results = []
    for name in os.listdir(root):
        full = os.path.join(root, name)
        cleaned = full.strip().lower()
        results.append(cleaned)
    return sorted(set(results))
