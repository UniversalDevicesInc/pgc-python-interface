[![Build Status](https://travis-ci.org/UniversalDevicesInc/pgc-python-interface.svg?branch=master)](https://travis-ci.org/UniversalDevicesInc/pgc-python-interface)

# UDI Polyglot v2 - Cloud Interface Module :: PGC

This is the Polyglot interface API module that is portable to be imported into your Python 3.4+ based NodeServers.

### Installation

pgc_interface attempts to maintain feature parity with poly_interface (the on-prem Polyglot interface API module) anything you can do there you should be able to do in the cloud.

Caveats:
```
* Notices are by keys only in pgc_ingterface, you can not do a list
* No custom config docs
* No typed params
```

### Starting your NodeServer build

When you start building a NodeServer you are helping build the free and open Internet of Things. Thank you! If you run in to any issues please ask your questions on the [UDI Polyglot Forums](http://forum.universal-devices.com/forum/111-polyglot/).

To get started, build your NodeServer using the on-premise version of Polyglot, you should not do builds in the cloud. [Use this link to get started on your build.](https://github.com/UniversalDevicesInc/polyglot-v2-python-interface/blob/master/README.md)

### Enabling your NodeServer to be installable in the cloud

Polyglot Cloud uses ephemeral docker images to run NodeServers. Nothing is persistent, so every time a NodeServer is stopped the docker is removed every time a NodeServer starts, a docker is run. We cut down on start times by instances 'pre-warmed' and ready for start calls. Due to this ephemeral nature, you should not save ANY necessary information to local storage. Use the standard customData or customParams methods to save state and or keys.

Enabling your NodeServer to install in the cloud is simple:
* Add an additional property to server.json "install_cloud"

[Polyglot NodeServer Template Example](https://github.com/Einstein42/udi-poly-template-python/blob/master/server.json)

By default a script is run when you install a NodeServer using the server.json's 'install' property. In the cloud you may have slightly different requirements. By default pgc_interface has to be your API module instead of on-prem's poly_interface.

Using the aforementioned Polyglot NodeServer Template Example we added 

"install_cloud": "install_cloud.sh"

install_cloud.sh:
```
#!/usr/bin/env bash
pip install -r requirements_cloud.txt
```

requirements_cloud.txt
```
pgc_interface>=1.0.0
```
We felt this level of granularity would allow developers some autonomy when it came to how certain things ran for each implementation. 

### Additional Cloud Methods and API's

#### Oauth2:
One of the best reasons to move a NodeServer to the cloud is for Oauth workflows. A lot (most?) of the cloud service providers use the Oauth2 workflow to exchange Keys and Authorization to their systems. We have enabled a mechanism to do this in the cloud for you.

When you create your service provider's integration, use the Oauth callback URL of:
* https://polyglot.isy.io/api/oauth/callback

This is the same for ANY oauth2 NodeServers. We simply pass-through using a state identifier below, so none of the information is ever saved by UDI.

When you enable this flow, you will provide UDI with the Oauth2 information that you will need for your NodeServer and it is sent back to your NodeServer in the init call that is sent to every NodeServer on startup. This is to prevent you having to store keys/secrets in your repository. Inside the init json message you will have an 'oauth' parameter that can have the following kinds of information:

* url: This will be the URL that you will have to point users to for initial authorization Example: https://home.nest.com/login/oauth2?client_id=XXXXXXXXXXX&state=
* clientId: This is the clientId for your integration via the cloud provider
* clientSecret: This is the clientSecret for your integration.

A normal Oauth2 flow in Polyglot cloud would look like this:

First install/start of a NodeServer:
* Check customData (contained in init) to see if tokens exist (they won't it's the first run)
* Take init.oauth url and add the workerId (docker instance id) to the 'state' in the oauth URL. From the example url above you would create the url like: `new_url = init.oauth.url + init.worker` this is VERY IMPORTANT. If you do not add the worker id to the url as the state, then Polyglot will have no way of knowing where to send the approved oauth code and will fail. 
* Add notice for users to click on the link you created in the previous step to initiate the oauth flow to authorize the integration.
* Users click on the notice link, authorize the app then are sent back to the dashboard
* Your NodeServer runs Controller class method called 'oauth', that you should override. A dictionary object is the only parameter to the method and it contains any reponse back from the authorizer. This typically only includes code and state, but if your authorizer returns other parameters, they will be included as well.

```
{
    code: "<code to use to get tokens>"
    state: "<the state worker you appended to the url>"
}
```
* Use the code returned to make the call to the provider to get your tokens
* Save tokens to customData for persistence

Normal start/stop (not first run):
* Check customData (contained in init) to see if tokens exist (they will since you went through the oauth2 flow)
* Use access_token or refresh_token to pull data as normal

#### netInfo:

Polyglot Cloud uses docker instances to run in the cloud. Some Cloud Providers don't use Oauth2 or other pull mechanisms (looking at you Rach.io) and use push mechanisms. This is hard to do for distributable on-premise NodeServers as typical home users are behind a router, firewall, nat, etc. However since we are running in the cloud each instance has a public IP address that can be used. 

In the inital init message that is sent to each NodeServer you will have a netInfo paramater that looks like this:

```
{
    publicDns: "<hostname>"
    publicIp: "<ip>"
}
```

This will allow you to do whatever is necessary INBOUND to your instance. Keep in mind that no services (including your NodeServer) are run as root on the docker image, so you cannot use ports < 1024.