'''
ABI Module.

ABI structure the "Functions" and "Events".

ABI also encode/decode params for functions.

See:
https://github.com/ethereum/wiki/wiki/Ethereum-Contract-ABI

"Function Selector":
sha3("funcName(uint256,address)") -> cut out first 4 bytes.

"Argument Encoding":

Basic:
uint<M> M=8,16,...256
int<M> M=8,16,...256
address
bool
fixed<M>x<N> fixed256x18
bytes<M> bytes32
function 20bytes address + 4 bytes signature.

Fixed length:
<type>[M] Fix sized array. int[10], uint256[33], 

Dynamic length:
bytes
string
<type>[]
'''

# voluptuous is a better library in validating dict.
from voluptuous import Schema, Any, Optional
from typing import List as ListType
from typing import Union
import eth_utils
import eth_abi


MUTABILITY = Schema(Any('pure', 'view', 'constant', 'payable', 'nonpayable'))


FUNC_PARAMETER = Schema({
        "name": str,
        "type": str
    },
    required=True
)


FUNCTION = Schema({
    "type": "function",
    "name": str,
    Optional("constant"): bool,
    "payable": bool,
    "stateMutability": MUTABILITY,
    "inputs": [FUNC_PARAMETER],
    "outputs": [FUNC_PARAMETER]
    },
    required=True
)


EVENT_PARAMETER = Schema({
    "name": str,
    "type": str,
    "indexed": bool
    },
    required=True
)


EVENT = Schema({
    "type": "event",
    "name": str,
    Optional("anonymous"): bool,
    "inputs": [EVENT_PARAMETER]
})


def is_dynamic_type(t: str):
    ''' Check if the input type is dynamic '''
    if t == 'bytes' or t == 'string' or t.endswith('[]'):
        return True
    else:
        return False


def calc_function_selector(abi_json: dict) -> bytes:
    ''' Calculate the function selector (4 bytes) from the abi json '''
    f = FUNCTION(abi_json)
    return eth_utils.function_abi_to_4byte_selector(f)


def calc_event_topic(abi_json: dict) -> bytes:
    ''' Calculate the event log topic (32 bytes) from the abi json'''
    e = EVENT(abi_json)
    return eth_utils.event_abi_to_log_topic(e)


class Coder():
    @staticmethod
    def encode_list(types: ListType[str], values) -> bytes:
        ''' Encode a sequence of values, into a single bytes '''
        return eth_abi.encode_abi(types, values)

    @staticmethod
    def decode_list(types: ListType[str], data: bytes) -> ListType:
        ''' Decode the data, back to a (,,,) tuple '''
        return list(eth_abi.decode_abi(types, data))
    
    @staticmethod
    def encode_single(t: str, value) -> bytes:
        ''' Encode value of type t into single bytes'''
        return Coder.encode_list([t], [value])

    @staticmethod
    def decode_single(t: str, data):
        ''' Decode data of type t back to a single object'''
        return Coder.decode_list([t], data)[0]


class Function():
    def __init__(self, f_definition: dict):
        '''Initialize a function by definition.

        Parameters
        ----------
        f_definition : dict
            See FUNCTION type in this document.
        '''
        self._definition = FUNCTION(f_definition) # Protect.
        self.selector = calc_function_selector(f_definition) # first 4 bytes.
    
    def encode(self, parameters: ListType, to_hex=False) -> Union[bytes, str]:
        '''Encode the paramters according to the function definition.

        Parameters
        ----------
        parameters : List
            A list of parameters waiting to be encoded.
        to_hex : bool, optional
            If the return should be '0x...' hex string, by default False

        Returns
        -------
        Union[bytes, str]
            Return bytes or '0x...' hex string if needed.
        '''
        my_types = [x['type'] for x in self._definition['inputs']]
        my_bytes = self.selector + Coder.encode_list(my_types, parameters)
        if to_hex:
            return '0x' + my_bytes.hex()
        else:
            return my_bytes
    
    def decode(self, output_data: bytes) -> dict:
        '''Decode function call output data back into human readable results.

        The result is in dual format. Contains both position and named index.
        eg. { '0': 'john', 'name': 'john' }
        '''
        my_types = [x['type'] for x in self._definition['outputs']]
        my_names = [x['name'] for x in self._definition['outputs']]

        result_list = Coder.decode_list(my_types, output_data)

        r = {}
        for idx, name in enumerate(my_names):
            r[str(idx)] = result_list[idx]
            if name:
                r[name] = result_list[idx]
        
        return r


class Event():
    def __init__(self, e_definition: dict):
        '''Initialize an Event with definition.

        Parameters
        ----------
        e_definition : dict
            A dict with style of EVENT.
        '''
        self._definition = EVENT(e_definition)
        self.signature = calc_event_topic(self._definition)
    
    def encode(self):
        pass

    def decode(self, data: bytes, topics: ListType[bytes]):
        ''' Decode "data" according to the "topic"s.

        One output can contain an array of logs[].
        One log contains mainly 3 entries:

        - For a non-indexed parameters event:

            "address": The emitting contract address.
            "topics": [
                "signature of event"
            ]
            "data": "0x..." (contains parameters value)

        - For an indexed parameters event:

            "address": The emitting contract address.
            "topics": [
                "signature of event",
                "indexed param 1",
                "indexed param 2",
                ...
                --> max 3 entries of indexed params.
            ]
            "data": "0x..." (remain un-indexed parameters value)

        If the event is "anonymous" then the signature is not inserted into the "topics" list,
        hence topics[0] is not the signature.
        '''
        if self._definition.get('anonymous', False) == False:
            # if not anonymous, topics[0] is the signature of event.
            # we cut it out, because we already have self.signature
            topics = topics[1:]
        
        _indexed_params_definitions = [x for x in self._definition['inputs'] if x['indexed']]
        _un_indexed_params_definitions = [x for x in self._definition['inputs'] if not x['indexed']]

        if len(_indexed_params_definitions) != len(topics):
            raise Exception('topics count invalid.')
            
        un_indexed_params = Coder.decode_list(
            [x['type'] for x in _un_indexed_params_definitions],
            data
        )

        r = {}
        for idx, each in enumerate(self._definition['inputs']):
            to_be_stored = None
            if each['indexed']:
                topic = topics.pop(0)
                if is_dynamic_type(each['type']):
                    to_be_stored = topic
                else:
                    to_be_stored = Coder.decode_single(each['type'], topic)
            else:
                to_be_stored = un_indexed_params.pop(0)

            r[str(idx)] = to_be_stored

            if each['name']:
                r[each['name']] = to_be_stored

        return r