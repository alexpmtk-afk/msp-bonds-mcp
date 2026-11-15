# VTB reports component

Current package: msp-bonds-vtb-reports 0.1.0. It supplies read-only archive inventory and PDF/XLSX format checks to the root MCP server; it does not start another server.

The source lives in src/msp_bonds_vtb_reports, offline synthetic tests in tests, and the optional local archive-root template in config/.env.example. The module uses only the Python standard library. It never writes, renames or deletes report files. The actual archive path is supplied through the local process environment and is not stored in this repository.

No real broker documents are included in the tests or package.
