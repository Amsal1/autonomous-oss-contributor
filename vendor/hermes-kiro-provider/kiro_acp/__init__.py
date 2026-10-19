"""Kiro CLI V3 ACP adapter for Hermes (fork of tobenwarrior/hermes-kiro-provider).

This package stays import-free on purpose: ``v3_core`` is stdlib-only and is imported by root code
(the ossbot Hermes-side probe) without loading Hermes, while ``v3`` and ``hermes_plugin`` hold the
Hermes provider glue and are imported only inside Hermes.
"""
