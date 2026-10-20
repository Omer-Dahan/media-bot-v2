"""Coverage for the variant-phrasing machinery in texts.py: tuple shape,
deterministic picking, resolve() boundary behaviour, and hopeless-failure
classification of rendered variants.
"""

from __future__ import annotations

import re

from media_bot_v2.telegram import texts
from media_bot_v2.telegram.retry import is_hopeless_failure

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_ALL_VARIANT_NAMES = [
    "DOWNLOADING_VARIANTS", "UPLOADING_VARIANTS", "PROCESSING_VARIANTS",
    "DOWNLOAD_FROM_CACHE_VARIANTS", "UNSUPPORTED_URL_VARIANTS", "NOT_MEDIA_CONTENT_VARIANTS",
    "INSTAGRAM_NOT_FOUND_VARIANTS", "FACEBOOK_NOT_FOUND_VARIANTS",
    "INSTAGRAM_PRIVATE_OR_LOGIN_VARIANTS", "FACEBOOK_PRIVATE_OR_LOGIN_VARIANTS",
    "DIRECT_LINK_REFUSED_VARIANTS", "DIRECT_LINK_GONE_VARIANTS", "DIRECT_LINK_SERVER_BUSY_VARIANTS",
    "DIRECT_FILE_TOO_LARGE_VARIANTS", "DOWNLOAD_FAILED_VARIANTS",
    "REQUEST_TIMEOUT_EXCEEDED_VARIANTS", "PING_RESULT_VARIANTS",
]


def test_variant_tuples_have_at_least_three_distinct_entries():
    for name in _ALL_VARIANT_NAMES:
        variants = getattr(texts, name)
        assert len(variants) >= 3, name
        assert len(set(variants)) == len(variants), name


def test_downloading_variants_have_no_leading_emoji_or_newline():
    for variant in texts.DOWNLOADING_VARIANTS:
        assert variant[0].isalpha() or variant[0] == '"', variant
        assert "\n" not in variant


def test_placeholder_sets_identical_within_each_tuple():
    for name in _ALL_VARIANT_NAMES:
        variants = getattr(texts, name)
        placeholder_sets = [frozenset(_PLACEHOLDER_RE.findall(v)) for v in variants]
        assert all(s == placeholder_sets[0] for s in placeholder_sets), name


def test_pick_is_stable_per_key_and_varies_across_keys():
    variants = texts.DOWNLOADING_VARIANTS
    assert texts.pick(variants, "same-key") == texts.pick(variants, "same-key")
    distinct = {texts.pick(variants, f"key-{i}") for i in range(50)}
    assert len(distinct) > 1


def test_resolve_handles_exact_prefixed_and_unknown_text():
    key = "test"
    exact = texts.resolve(texts.DOWNLOADING, key)
    assert exact in texts.DOWNLOADING_VARIANTS

    with_progress = texts.resolve(f"{texts.DOWNLOADING}\n45%", key)
    assert with_progress.split("\n")[0] in texts.DOWNLOADING_VARIANTS
    assert with_progress.endswith("\n45%")

    with_arrow_prefix = texts.resolve(f"⬇️ {texts.DOWNLOADING}\n45%", key)
    assert with_arrow_prefix.startswith("⬇️ ")
    assert with_arrow_prefix[len("⬇️ "):].split("\n")[0] in texts.DOWNLOADING_VARIANTS

    unknown = "this text is not a known canonical status or error"
    assert texts.resolve(unknown, key) == unknown


def test_rendered_hopeless_variant_is_still_hopeless():
    for variant in texts.UNSUPPORTED_URL_VARIANTS + texts.INSTAGRAM_PRIVATE_OR_LOGIN_VARIANTS:
        assert is_hopeless_failure(text=variant)
