# Changelog

All notable user-visible changes to `bili-album` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses Semantic Versioning.

## [Unreleased]

### Added

- Added millisecond refinement of the cuts between tracks, so a soft intro is no longer clipped by a cut a few hundred milliseconds late (`--no-refine` turns it off).
- Added a 切点精修 menu at the top right of the web page: turn millisecond refinement off for the next links, or change its search window, silence thresholds and pre-roll.
