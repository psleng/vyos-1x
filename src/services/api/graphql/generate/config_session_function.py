# typing information for native configsession functions; used to generate
# schema definition files
import typing

def show_config(path: list[str], configFormat: typing.Optional[str]):
    pass

def show(path: list[str]):
    pass

def show_user_info(user: str):
    pass
def show_cellular_firmware(interface: str):
    pass

queries = {'show_config': show_config,
           'show': show,
           'show_user_info': show_user_info,
           'show_cellular_firmware': show_cellular_firmware}

def save_config_file(fileName: typing.Optional[str]):
    pass
def load_config_file(fileName: str):
    pass
def add_system_image(location: str):
    pass
def set_cellular_firmware(version: str, interface: typing.Optional[str]):
    pass
def delete_cellular_firmware(version: str, interface: typing.Optional[str]):
    pass
def delete_system_image(name: str):
    pass

mutations = {'save_config_file': save_config_file,
             'load_config_file': load_config_file,
             'add_system_image': add_system_image,
             'set_cellular_firmware': set_cellular_firmware,
             'delete_cellular_firmware': delete_cellular_firmware,
             'delete_system_image': delete_system_image}
