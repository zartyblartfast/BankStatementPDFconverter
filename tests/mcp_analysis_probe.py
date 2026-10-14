"""Actual MCP client/server handshake and tool exercise, against an explicit fixture only."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from jsonschema import validate


async def probe(root, allow_details=False, confirmed=False):
    repo = Path(__file__).resolve().parents[1]
    arguments = ["-m", "scripts.agent_mcp", "--root", str(root)]
    if allow_details:
        arguments.append("--allow-transaction-details")
    params = StdioServerParameters(command=sys.executable, args=arguments, cwd=str(repo))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()
            names = {tool.name for tool in discovered.tools}
            required = {"get_data_status", "list_categories", "search_transactions", "summarise_transactions"}
            assert names == required, names
            assert all(
                t.annotations and t.annotations.readOnlyHint and not t.annotations.destructiveHint
                for t in discovered.tools
            )
            results = {}
            for name in sorted(required):
                result = await session.call_tool(name, {})
                assert not result.isError, (name, result)
                body = result.structuredContent
                assert body and body["meta"]["schema_version"] == "1.1"
                assert body['meta']['calculation_basis'] == ('transfer_adjusted' if name == 'summarise_transactions' else 'raw')
                tool = next(t for t in discovered.tools if t.name == name)
                assert tool.outputSchema is not None
                validate(body, tool.outputSchema)
                assert body["meta"]["money_unit"] == "pence"
                assert body["meta"]["transaction_details_enabled"] is allow_details
                assert not body["meta"]["contains_transaction_details"]
                results[name] = body
            assert "transactions" not in results["search_transactions"]["data"]
            assert results["get_data_status"]["data"]["transaction_count"] == (3 if confirmed else 2)
            assert results["search_transactions"]["data"]["credits_pence"] == 60000
            summary = results['summarise_transactions']['data']
            assert summary['debits_pence'] == 10000
            assert summary['credits_pence'] == (0 if confirmed else 60000)
            assert summary['raw']['debits_pence'] == (70000 if confirmed else 10000)
            assert summary['transfers']['out_count'] == (1 if confirmed else 0)
            assert summary['transfers']['in_count'] == (1 if confirmed else 0)
            assert summary['adjusted'] == {k: summary[k] for k in ('credits_pence','debits_pence','net_pence')}
            assert 'transactions' not in summary and 'txn:' not in json.dumps(summary)
            detail = await session.call_tool("search_transactions", {"include_details": True})
            if allow_details:
                assert not detail.isError, detail
                body = detail.structuredContent
                assert body and body["meta"]["contains_transaction_details"]
                assert len(body["data"]["transactions"]) == (3 if confirmed else 2)
                assert all(row["description_untrusted"] == "Synthetic" for row in body["data"]["transactions"])
                validate(body, next(t.outputSchema for t in discovered.tools if t.name == "search_transactions"))
            else:
                assert detail.isError, "Default server must reject raw transaction disclosure"
            invalid_results = []
            for invalid in ({"amount": "NaN"}, {"start": "invalid"}, {"page_size": 101}):
                failed = await session.call_tool("search_transactions", invalid)
                assert failed.isError, invalid
                invalid_results.append(failed)
            absent = await session.call_tool("recategorize", {})
            assert absent.isError, "Write tool must not exist"
            return {
                "handshake": "ok",
                "tools": sorted(names),
                "aggregate_calls": len(results),
                "details_denied": detail.isError,
                "invalid_inputs_rejected": len(invalid_results),
                "write_tool_absent": absent.isError,
            }


async def both_modes(root, confirmed=False):
    default = await probe(root, confirmed=confirmed)
    default["opted_in"] = await probe(root, allow_details=True, confirmed=confirmed)
    return default


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    print(json.dumps(asyncio.run(both_modes(args.fixture_root, args.confirmed))))
