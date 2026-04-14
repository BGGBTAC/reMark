# Example plugins

Drop any `*.py` in this directory into your
`plugins.plugin_dir` (default `~/.config/remark/plugins`) to enable it.
None of these ship in the default distribution — they live here as
reference implementations.

## math_latex_plugin.py

Auto-wraps bare LaTeX fragments in `$...$` (or `$$...$$` for
`\begin{equation}` blocks) so Obsidian renders them as math. Pure text
pass, no image access needed.

**Install:**
```bash
cp math_latex_plugin.py ~/.config/remark/plugins/
```

**Settings (optional, in `config.yaml`):**
```yaml
plugins:
  enabled: true
  settings:
    math_latex:
      wrap_inline: true
      wrap_block: true
    math_latex_ocr:
      backend: pix2text         # or "mathpix"
      mathpix_app_id_env: MATHPIX_APP_ID
      mathpix_app_key_env: MATHPIX_APP_KEY
```

The separate `math_latex_ocr` class is an optional OCR backend that
delegates to `pix2text` (`pip install pix2text`) or the MathPix API.
It ships as a stub: the engine doesn't hand individual page images to
OCR plugins yet — that wiring is planned for a follow-up release. For
now it's safe to enable; if the engine never calls it, nothing happens.
