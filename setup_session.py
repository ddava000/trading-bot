"""
Run this once locally to generate your Robinhood session token.
Writes the base64 session to session_output.txt when done.
"""
import base64, os, getpass
import robin_stocks.robinhood as rh

print("="*60)
print("Robinhood Session Setup")
print("="*60)
username = input("Robinhood email: ")
password = getpass.getpass("Robinhood password: ")

print("\nLogging in...")
rh.login(username, password, store_session=True, expiresIn=86400*30)

pickle_path = os.path.join(os.path.expanduser("~"), ".tokens", "robinhood.pickle")
with open(pickle_path, "rb") as f:
    encoded = base64.b64encode(f.read()).decode()

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_output.txt")
with open(out_path, "w") as f:
    f.write(encoded)

print("\nSUCCESS! Session saved to session_output.txt")
print("You can close this window now.")
input("\nPress Enter to close...")
