from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from dotenv import load_dotenv
import os

load_dotenv()

client = ClobClient(
    'https://clob.polymarket.com',
    key=os.getenv('PRIVATE_KEY'),
    chain_id=137,
)

# Derive L2 creds (required for these calls)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

# Update USDC (collateral) allowance
print('Setting USDC allowance...')
resp1 = client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print('USDC:', resp1)

# Update conditional token allowance
print('Setting conditional token allowance...')
resp2 = client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
print('Conditional:', resp2)

# Verify
print('Checking USDC balance/allowance...')
bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print('Balance:', bal)

print('Done!')
