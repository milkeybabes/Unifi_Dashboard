# Camera compatibility notes

## G6 Instant and AV1

During testing, a G6 Instant using Protect `Advanced` encoding returned an AV1 stream (`av01...`).

Desktop Chrome could play it, while the tested iPad WebKit media path could not and correctly fell back to snapshots.

Changing Protect:

```text
Recording Quality -> Encoding -> Standard
```

made the camera play live on iPad immediately.

No transcoding was added.

## G6 Entry package camera

The tested G6 Entry reports:

```text
hasPackageCamera: true
channels:
  High
  Medium
  Low
  Package Camera
```

A direct probe confirmed:

```text
primary     -> main/front camera
lens 1      -> main/front camera again
lens 2      -> package camera
```

The confirmed package stream was:

```text
3264 x 2448
3 FPS
HEVC / H.265
```

The dashboard therefore requests Protect secondary `lens 2` for the G6 Entry package-camera inset.

The package snapshot is also distinct from the primary-camera snapshot.
