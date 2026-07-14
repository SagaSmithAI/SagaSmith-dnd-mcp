# Smoke Test: The Split Lantern

## Lantern Room

The party arrives at a locked lantern room. The brass key is hidden beneath a
loose tile, and the east door opens onto a small training yard.

### Scene

- `scene_id`: `smoke-lantern-room`
- `scope_id`: `party`
- `status`: `current`

## Training Yard

The yard contains a friendly sparring post and one hostile practice automaton.
This scene is intentionally deterministic and exists only for runtime smoke
tests; it is not an adventure module.

### Scene

- `scene_id`: `smoke-training-yard`
- `scope_id`: `party`
- `status`: `future`
