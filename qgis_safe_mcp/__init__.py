def classFactory(iface):
    from .plugin import SafeMcpPlugin

    return SafeMcpPlugin(iface)
