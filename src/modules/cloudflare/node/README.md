# Cloudflare Tunnel – node part

Automatically distributed to a node by the "Cloudflare Tunnel" module as soon as a route
"RDP/SSH/VNC to an agent" with *tunnel ends on a node* is created.
Each node gets its own Cloudflare tunnel (`rmm-node-…`); the connector runs as a
container `rmm-cloudflared` with `network_mode: host` so it can reach the devices in the node's network.
Nothing to configure by hand – the module maintains the token and routes.
