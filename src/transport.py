import usb.core
import usb.util

ID_VENDOR = 0x057e
ID_PRODUCT = 0x3000

def connect_switch():
    dev = usb.core.find(idVendor=ID_VENDOR, idProduct=ID_PRODUCT)
    if dev is None:
        raise ConnectionError("Console not found.")

    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)

    dev.set_configuration()
    cfg = dev.get_active_configuration()
    intf = cfg[(0,0)]

    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)

    if not ep_out or not ep_in:
        raise ConnectionError("Endpoint not found.")

    return dev, ep_out, ep_in