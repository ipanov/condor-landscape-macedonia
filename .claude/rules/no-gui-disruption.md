---
description: Prevent desktop disruption from GUI tools
globs: scripts/**/*.py
---

# No Desktop Disruption

Never launch GUI applications (RawToTrn.exe, LandscapeEditor.exe, ObjectEditor.exe) without explicit user approval. The only confirmed safe CLI command is:

```
tools/CLT2.7/LandscapeEditor.exe -hash MacedoniaSkopje
```

Condor 2 runs as an elevated process. Standard input injection is blocked by UIPI. For visual testing, ask the user to perform clicks, then take screenshots with the MCP screenshot_control tool.
