# Everest API MCP Server

A lightweight MCP server for querying the Validity Everest API.

This server provides 3 tools:
- `everest_query_v1`: full-domain aggregate query
- `everest_query_v2`: subdomain-only query with cross-TLD filtering
- `everest_query_batch`: batch query in `v1` or `v2`

## Requirements

- Python 3.9+
- Everest API key

## Quick Deploy

1. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

2. Set your API key:

```bash
export EVEREST_API_KEY="your_api_key"
```

3. (Optional) Set transport (default is `streamable-http`):

```bash
export MCP_TRANSPORT="streamable-http"
```

4. Start the server:

```bash
python3 everest_api.py
```

## Notes

- API key priority:
  1) tool argument `api_key`
  2) environment variable `EVEREST_API_KEY`
- Main endpoint base URL: `https://api.everest.validity.com/api/2.0`

## License

MIT (see `LICENSE`).
