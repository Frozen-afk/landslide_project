"""Landslide volume estimation from overlapping phone photos.

Pipeline: SfM reconstruction (pycolmap) -> metric scale from a reference
marker (ArUco auto-detect or manual 2-view clicks) -> semi-dense multi-view
stereo -> user polygon selection -> volume between surface and rim datum.
"""

__version__ = "0.1.0"
