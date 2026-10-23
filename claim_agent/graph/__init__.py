"""Three-layer graphs for the claim pipeline.

1. rules      — sources/*.md split into sections, gates and conditions; role×scope rule packs (token saving).
2. invention  — the current invention's components, features, problems, mechanisms and effects; every node and
                edge carries a verbatim span of the raw material, otherwise it is not in the graph.
3. precedent  — registered Korean prosecution histories (cs2-spec-writer examples.json): which kinds of added
                limitations survived an office action. Read only by the dependent-claim side (design doc §13 #10).

Nothing here is technical evidence by itself: layer 2 points back to the raw material, and every idea the graph
algorithms produce is labelled `INFERRED — 근거 미확인`.
"""
