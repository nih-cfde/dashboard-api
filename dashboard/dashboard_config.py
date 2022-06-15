DEBUG = False
SHOW_NULLS = True
PASS_HEADERS = True

# Set to 'localhost' for deployment file so the API finds deriva on the same host.
# When developing on a stack without deriva, use "app-dev.nih-cfde.org"
DERIVA_SERVERNAME = "localhost"
DERIVA_DEFAULT_CATALOGID = "1"

# Note: This prop is used for local testing to where the chaise navbar is not part of the stack
# When testing locally, there won't be an auth token which is passed through the API to cfde-deriva
# In order to create one for local testing:
# 1) Login to dev site with your browser app-dev.nih-cfde.org (must be VPN'd using the AWS/CFDE VPN)
# 2) Get the webauthn cookie value from your browser
# 3) Drop it in as string value for DEV_TOKEN (below)
# DO NOT push a DEV prop file with a token value to the repo
DEV_TOKEN = ""
