---
name: reverse-image-search
description: Find where a photo appears online (reverse image search). Stub - no provider is wired yet.
version: 0.0.1
---

# Reverse image search (stub)

Status: not available. ossbot has a documented key hook for a reverse image search provider
(`REVERSE_IMAGE_SEARCH_KEY`, set with `scripts/setup-secrets.*`; DESIGN §14.12.7, §15.6), but no
provider (SerpAPI, Bing Visual Search, Google Lens, ...) is implemented until the owner picks one.

When the owner asks where an image comes from, or to find similar images:

1. Say that reverse image search is not set up yet and that it needs a provider choice and key.
2. Offer what works today: describe the image, read visible text, and suggest search terms the owner
   can try with `web_search`.
3. Never upload the image anywhere yourself.

This file ships in the repository only. It is not installed into Hermes' active skills directory by
provisioning; once a provider is chosen, the implemented skill goes through the skill gate
(DESIGN §15.5) like every other skill.
