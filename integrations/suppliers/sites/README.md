# Supplier site configurations (ADR-0030)

One JSON file per supplier on a platform template, named `<supplier_key>.json`. A site holds no code: only plain words, host names and numbers, validated by `integrations/suppliers/site_config.py` and bound to its template by `integrations/suppliers/sites.py`. A file that fails validation leaves its site out and stops nothing else.

```json
{
  "schema": "icbm-supplier-site/v1",
  "supplier_key": "example",
  "display_name": "Example",
  "platform": "cafe24",
  "base_url": "https://example.co.kr",
  "storefront_host": "example.co.kr",
  "image_hosts": ["example.co.kr"],
  "product_path_form": "seo",
  "label_overrides": {"price": ["도매가"]},
  "region_overrides": {"detail": {"by": "id", "token": "prdDetail"}},
  "limits": {"max_image_refs": 20},
  "status": "RECON",
  "revision": "example-1"
}
```

Optional keys: `extra_egress_hosts`, `label_overrides`, `region_overrides`, `limits`, `limit_decision` (the owner's decision reference, needed for a limit above the template default), `seller_code_convention` (owner-declared, ADR-0024 §2), `recon_record` (required for `ACTIVE`).

Rules:
- Every host was observed in the site's reconnaissance record (`documents/acceptance/suppliers/<key>-recon.md`). No wildcard.
- Label overrides add words to a template slot; region overrides move a template region. Both use only the template's own slot names.
- A `RECON` site collects, but its products cannot be prepared for a marketplace. `ACTIVE` is set only by a reviewed PR that cites the site's acceptance record.
- Any semantic change advances `revision`.
- No credential, cookie or account name ever appears here. The owner types the account in the settings UI.
