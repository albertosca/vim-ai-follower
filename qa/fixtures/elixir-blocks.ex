defmodule Graph.Traversal do
  @moduledoc """
  Breadth-first traversal over an adjacency map.
  """

  @type graph :: %{optional(atom()) => [atom()]}

  @spec reachable(graph(), atom()) :: [atom()]
  def reachable(graph, from) do
    walk(graph, :queue.from_list([from]), MapSet.new([from]), [])
  end

  defp walk(graph, queue, seen, acc) do
    case :queue.out(queue) do
      {{:value, node}, rest} ->
        neighbours =
          graph
          |> Map.get(node, [])
          |> Enum.reject(&MapSet.member?(seen, &1))

        walk(
          graph,
          Enum.reduce(neighbours, rest, &:queue.in/2),
          Enum.into(neighbours, seen),
          [node | acc]
        )

      {:empty, _rest} ->
        Enum.reverse(acc)
    end
  end
end
