# Third-party notices

AutoSkyPlay is a Python rewrite based on the features and public source of
[Sky-MusaAutoPlay-SMAP-](https://github.com/lingyunalingyun/Sky-MusaAutoPlay-SMAP-).
Reference revision: `0416e82fa95c472de2de7eaf96ddd05445db73fc`.

The upstream project is licensed under GNU GPL v3. This project preserves that
license; see LICENSE. Application logic and desktop UI were rewritten in Python.
No C# or Java executable/runtime is required.

The following data assets are retained from upstream:

- `src/autoskyplay/assets/instruments/`: 555 WAV files in 37 instrument banks.
  Upstream attributes samples to Sky Studio and sounds extracted from Sky.
  Rights to game sounds remain with their respective owners; inclusion in the
  upstream repository does not establish a separate grant from those owners.
- `src/autoskyplay/assets/logo.png` and `app.ico`: upstream application branding.
- `src/autoskyplay/assets/translations.json`: translation strings extracted from
  upstream `Lang.cs`.

The third-party song collection is not bundled. Importing a score does not
transfer its copyright. The bundled `exercise.json` is an original exercise
created for AutoSkyPlay.

Python dependencies retain their own licenses. PySide6/Qt are used through their
open-source distribution; their notices accompany PyInstaller bundles. Review
asset rights and dependency license obligations before public distribution.
