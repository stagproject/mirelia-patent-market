import os
import requests
import json
import time
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from supabase import create_client, Client
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp = FastMCP("Mirelia-Patent-Marketplace", transport_security=security_settings)

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(url, key)

rpc_url = os.environ.get("META_RPC_URL")
private_key = os.environ.get("META_PRIVATE_KEY")
if rpc_url and private_key:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    account = w3.eth.account.from_key(private_key)
    WALLET_ADDRESS = account.address
else:
    w3 = None
    WALLET_ADDRESS = None

def get_rose_usd_price():
    res = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=oasis-network&vs_currencies=usd")
    res.raise_for_status()
    return float(res.json()["oasis-network"]["usd"])

@mcp.tool()
def list_available_categories() -> str:
    """Returns a JSON string containing an array of all available patent categories."""
    res = supabase.table("v_catalogs").select("category").execute()
    categories = sorted(list(set([item['category'] for item in res.data]))) if res.data else []
    return json.dumps({"available_categories": categories}, ensure_ascii=False)

@mcp.tool()
def get_packages_by_category(category: str) -> str:
    """Returns a JSON string containing an array of patent packages in the specified category."""
    res = supabase.table("v_catalogs").select("package_tag, title, record_count, price_usd, sales_count").eq("category", category).execute()
    data = res.data if res.data else []
    if isinstance(data, dict): data = [data]
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def get_package_details(package_tag: str) -> str:
    """Returns a JSON string with the full details of a specific patent package."""
    res = supabase.table("v_catalogs").select("*").eq("package_tag", package_tag).execute()
    data = res.data[0] if res.data else {}
    return json.dumps(data, ensure_ascii=False)

@mcp.tool()
def verify_crypto_payment_and_deliver(tx_hash: str, package_tag: str) -> str:
    """
    Verifies payment and delivers the data. 
    Returns a JSON object with 'system_log' and 'package_data' array.
    """
    if not w3 or not w3.is_connected(): 
        return json.dumps({"error": "Web3 connection failed"}, ensure_ascii=False)

    try:
        try:
            tx = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return json.dumps({"error": "Invalid transaction hash"}, ensure_ascii=False)

        if tx['to'].lower() != WALLET_ADDRESS.lower():
            return json.dumps({"error": "Invalid destination wallet address"}, ensure_ascii=False)

        if receipt['status'] != 1:
            return json.dumps({"error": "Transaction failed on-chain"}, ensure_ascii=False)

        # ========== テスト用（時間チェック無効化）：ここから ==========
        # block = w3.eth.get_block(receipt['blockNumber'])
        # pass 
        # ========== テスト用（時間チェック無効化）：ここまで ==========

        # ========== 本番用（時間チェックあり）：ここから ==========
        block = w3.eth.get_block(receipt['blockNumber'])
        current_time = int(time.time())
        if current_time - block['timestamp'] > 3600:
            return json.dumps({"error": "Transaction is expired. Must be executed within the last 1 hour."}, ensure_ascii=False)
        # ========== 本番用（時間チェックあり）：ここまで ==========

        catalog_res = supabase.table("v_catalogs").select("price_usd, sales_count").eq("package_tag", package_tag).execute()
        if not catalog_res.data:
            return json.dumps({"error": "Package not found"}, ensure_ascii=False)
            
        catalog_data = catalog_res.data[0] if isinstance(catalog_res.data, list) else catalog_res.data
        real_price_usd = float(catalog_data['price_usd'])
        current_sales = catalog_data['sales_count'] or 0
        rose_usd_rate = get_rose_usd_price()
        
        # ========== テスト用（金額チェック無効化）：ここから ==========
        # log_msg = f"[TEST MODE] Payment of ${real_price_usd} verified (Amount check bypassed). Sales count updated to {current_sales + 1}."
        # ========== テスト用（金額チェック無効化）：ここまで ==========

        # ========== 本番用（金額チェックあり）：ここから ==========
        required_wei = w3.to_wei(real_price_usd / rose_usd_rate, 'ether')
        if tx['value'] < required_wei * 0.90:
             return json.dumps({"error": f"Insufficient funds. Price is ${real_price_usd}"}, ensure_ascii=False)
             
        log_msg = f"Payment of ${real_price_usd} verified. Sales count updated to {current_sales + 1}."
        # ========== 本番用（金額チェックあり）：ここまで ==========

        supabase.table("patent_packages").update({"sales_count": current_sales + 1}).eq("package_tag", package_tag).execute()
        res_data = supabase.table("v_patent_marketplace_lite").select("*").contains("package_tags", [package_tag]).execute()
        deliver_data = res_data.data if isinstance(res_data.data, list) else [res_data.data] if res_data.data else []
        
        return json.dumps({
            "system_log": log_msg,
            "package_data": deliver_data
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    if hasattr(mcp, "get_starlette_app"):
        app = mcp.get_starlette_app()
    elif hasattr(mcp, "sse_app"):
        app = mcp.sse_app()
    else:
        raise RuntimeError("FastMCP instance has no recognizable app method.")
    uvicorn.run(app, host="0.0.0.0", port=port)
