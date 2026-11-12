# UBX/NMEA importer

GNSS NMEA/UBX and GPX imports discard timestamp outliers. They keep the largest
group of distinct timestamps connected by gaps of at most 24 hours; equal-size
groups prefer the earliest group. Continuous recordings can span multiple days.
The same retained time range applies to all channels, and skipped counts are
recorded in import metadata. The NMEA/UBX importer also skips NMEA sentences
containing undecodable binary data while keeping checksum validation enabled.
Reimport existing HDF5 caches to apply these rules to previously imported files.

Both GNSS NMEA/UBX and GPX imports also create `GNSS/distance_3d_m` and
`GNSS/distance_2d_m`: cumulative travelled distance in metres, starting at zero.
The 2D series sums great-circle distances on a mean-radius Earth and ignores
altitude; the 3D series combines each horizontal step with its altitude change.
If either endpoint lacks altitude, that step uses horizontal distance for both
series. Repeated timestamps count as one position (the last report), timestamp
outliers are removed before calculation, and gaps between GPX segments add no
distance. Reimport existing caches to add these series.

NMEA `GNSS/fix_quality` opens as a named state lane in a Multi-series card: GNSS fix, Diff GNSS fix,
PPS fix, RTK fixed, and RTK float (codes 1–5). Right-click the series and choose
**Line** for its numeric plot, or **Event/state** for its state plot.
UBX NAV-PVT fix codes are stored separately as `GNSS/fix_type` with their own
labels because they use a different numbering scheme. Reimport existing caches
to add the labels and separate UBX fix types.

## Installation and compatibility

Plugin version 0.1.0 supports [LODGaZilla](https://github.com/RikuRaitis/LODGaZilla)
plugin API 1. This is an independent repository; its installation directory
must use the manifest ID `pyubx2_importer`. With the application checked out at
`HOST`, install it using:

```powershell
git clone https://github.com/RikuRaitis/LODGaZilla-pyubx2_importer.git "HOST/plugin/pyubx2_importer"
python "HOST/tools/setup_environment.py"
```

Enable it in the application's **Settings > Plugins**. `requirements.txt` declares
direct dependencies; `constraints.txt` records versions tested on Windows/Python 3.11.
The application and plugin requirements are resolved together in the Python
installation running the setup helper; no virtual environment is created.
The application can run without this plugin. Update this checkout
explicitly with `git pull --ff-only`, then rerun dependency setup in the host.
Plugin changes are committed and pushed here, not in the application repository.

Plugin settings, where present, load committed `settings.json` defaults and save
user changes to ignored `settings.local.json`. Keep captured input files, logs
and personal settings out of the repository.

## Tests

From any independent plugin checkout, point the host's runner at this directory:

```powershell
python "HOST/tools/run_tests.py" --host "HOST" --plugin "." --isolated
```

Use the same Python installation for dependency setup, the application and tests.
The runner stages this plugin under its stable ID and
supplies the host path; tests never rely on sibling plugin repositories. Integration
tests use the real host API, with synthetic data and no connected hardware.
Add `--all-plugins` to validate alongside other plugins installed in HOST.

## License

Copyright (C) 2026 Riku Tervonen

This plugin is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed in
the hope that it will be useful, but without any warranty; without even the implied
warranty of merchantability or fitness for a particular purpose. See [LICENSE](LICENSE)
for the full text (SPDX: `GPL-3.0-or-later`). Its dependencies keep their own licenses.
