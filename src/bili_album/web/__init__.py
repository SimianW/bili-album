"""Web page for bili-album: paste a Bilibili link, the album lands in Jellyfin.

``server`` serves one page and a small JSON API, ``jobs`` keeps the queue and
the per-job ``job.json`` files, ``runner`` adapts the processing pipeline (or
the scripted ``fake`` runner) to the job manager.
"""
