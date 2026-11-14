"""Native group-image protobuf shared by scheduled and keyword replies."""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from astrbot.core.platform.sources.wangshangliao import wire

# Official 3.7.1: Message.image=101, format=1; Image.asset=1 and Asset.uri=1.
# An independent descriptor keeps the existing live text codec unchanged.
_schema = descriptor_pb2.FileDescriptorProto()
_schema.ParseFromString(wire.ApplicationMessage.DESCRIPTOR.file.serialized_pb)
_schema.name = "wangshangliao-feature-image.proto"
_asset = _schema.message_type.add(name="GuideAsset")
_asset.field.add(name="uri", number=1, type=9, label=1)
_image = _schema.message_type.add(name="GuideImage")
for _name, _number, _type in (
    ("asset", 1, 11),
    ("width", 2, 5),
    ("height", 3, 5),
    ("size", 4, 5),
    ("is_sticker", 5, 8),
):
    _field = _image.field.add(name=_name, number=_number, type=_type, label=1)
    if _name == "asset":
        _field.type_name = ".wsl.GuideAsset"
for _message in _schema.message_type:
    if _message.name == "ApplicationMessage":
        _message.field.add(
            name="image", number=101, type=11, label=1, type_name=".wsl.GuideImage"
        )
_pool = descriptor_pool.DescriptorPool()
_pool.Add(_schema)
ImageMessage = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName("wsl.ApplicationMessage")
)
