"""Interface adapters over the textflowkit core.

Adapters are deliberately thin: they translate a transport into a call on
`textflowkit.core.pipeline.transcribe` and translate the result back. No
pipeline logic lives here.
"""
