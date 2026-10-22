# Documentation illustration assets

`assistant-overview.svg` is a self-contained repository-native SVG. The robot,
layout, arrows and labels are editable vector elements. Product icons are
embedded, so the overview does not depend on external image requests when
rendered in a README. Original icon files are kept in `icons/`.

| File | Source |
| --- | --- |
| `icons/google-sheets.png` | [Google-hosted Sheets product icon](https://www.gstatic.com/images/branding/product/2x/sheets_2020q4_48dp.png) |
| `icons/firestore.svg` | `firestore/firestore.svg` from the [official Google Cloud legacy console icon archive](https://services.google.com/fh/files/misc/google-cloud-legacy-icons.zip), linked by the [Google Cloud icon library](https://cloud.google.com/icons) |
| `icons/google-calendar.png` | [Google-hosted Calendar product icon](https://www.gstatic.com/images/branding/product/2x/calendar_2020q4_48dp.png) |

Product marks belong to Google and identify the integrated services. Icon
colors/shapes are retained. Embedded Firestore CSS/element IDs are namespaced
only to avoid collisions with the containing illustration. These assets do not
imply endorsement or change the repository's source licensing status.

When changing the flow, update the SVG title/description and README alt text.
Inspect the rendered overview at its760px native width and a380px mobile width.
