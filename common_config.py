from dataclasses import dataclass
from dataclasses_json import dataclass_json


@dataclass_json
@dataclass
class CommonConfig:
    rouge_like: bool = False
    sanity_mode: str = 'grass'


common_config = CommonConfig()
