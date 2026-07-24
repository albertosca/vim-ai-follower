"""
Breadth-first search over an adjacency-list graph.
Demonstrates a recognizable algorithm animating line-by-line.
"""
from collections import deque


def bfs(graph, start):
    """
    Traverse the graph starting from a node, visiting all reachable nodes
    in breadth-first order.

    Args:
        graph: dict mapping node IDs to lists of neighbors.
        start: the starting node ID.

    Returns:
        list of nodes in BFS order.
    """
    visited = set()
    queue = deque([start])
    result = []

    while queue:
        node = queue.popleft()
        if node not in visited:
            visited.add(node)
            result.append(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    queue.append(neighbor)

    return result


if __name__ == "__main__":
    sample = {
        "A": ["B", "C"],
        "B": ["A", "D"],
        "C": ["A", "E"],
        "D": ["B"],
        "E": ["C", "F"],
        "F": ["E"],
    }
    traversal = bfs(sample, "A")
    print(f"BFS order: {traversal}")
