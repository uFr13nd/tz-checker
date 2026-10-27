This folder is used by the application at runtime.
- settings.json: provider settings. On Windows the API key is encrypted with
  DPAPI (api_key_enc); on other systems the file itself must be protected.
- tz_checker.sqlite3: standards library, embeddings cache, run history and audit.
- tz-checker.log: rotating server log.
Do not share these files or upload them to a repository.
