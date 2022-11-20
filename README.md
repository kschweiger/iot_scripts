# IOT scripts

Collection of IoT scripts running together with the [data_receiver](#) API.

## Enviro+ 

The script `enviro_monitor.py` is intended as a background processes running for continous data collection. The data will be sent via requests to a remote API waiting for the data.

### Setup

In addition the to the packages listed in the `requirements.txt` file, it is expect that the 
[Enviro setup](https://learn.pimoroni.com/article/getting-started-with-enviro-plus) has been followed and the packages are installed.

Also create a file called `.secrets.toml` with the following information:

```toml
dynaconf_merge=true

[receiver]
host="XXX.XXX.XXX.XXX"

[enviro_monitor]
api_key="XXXXXX"
```




