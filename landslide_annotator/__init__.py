def classFactory(iface):
    from .landslide_annotator import LandslideAnnotatorPlugin
    return LandslideAnnotatorPlugin(iface)
