# Map Coordinate Contract

- Canonical database coordinates are world coordinates in metres; pixels are never persisted as navigation truth.
- Default `frame_id` is `map`.
- `x` increases toward the map right/east axis. `y` increases toward the map top/north axis.
- `theta` is radians; zero points along positive `x`; positive rotation is counter-clockwise in world space.
- `origin_x`, `origin_y` locate the lower-left image/world origin before map rotation.
- `rotation_rad` rotates local map coordinates counter-clockwise into the displayed world frame.
- Image pixels use a top-left origin and positive Y downward. `MapAdapter` performs the final Y flip.
- `resolution_m_per_pixel` defines image scale. Background assets are addressed by random object name and SHA-256.

For an unrotated map with screen scale `s` and fitted offsets `ox/oy`:

```text
screen_x = ox + (world_x - origin_x) * s
screen_y = oy + (height_m - (world_y - origin_y)) * s
```

The inverse transform is tested in `apps/web/tests/map-adapter.test.ts`, including rotation.

Every pose, polygon, inspection point, extinguish point and trajectory references a `map_version_id`. A task stores `map_id_snapshot`, `map_version_snapshot`, `semantic_revision_snapshot`, target pose and optional trajectory. A Published map is immutable; edits produce a Draft/new version. Dispatch is rejected when the robot map version differs from the target Published version.
