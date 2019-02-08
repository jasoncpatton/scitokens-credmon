from credmon.CredentialMonitors.OAuthCredmonWebserver import app

# Uncomment and change the below to a long, random string.
# This will enable users' sessions to persist even if the process
# restarts unexpectedly. Otherwise, a random secret_key will
# be generated every time the process starts.

#app.secret_key = 'ChangeThisInYourApplication'

application = app
