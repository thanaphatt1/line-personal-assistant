"""Tool-owned recovery metadata; error names are diagnostics, not policy."""

KINDS={'assistant_arguments','owner_input','unsupported','unavailable'}


class PlanningError(ValueError):
    def __init__(self,code,*,kind='assistant_arguments',fields=(),required=(),tools=()):
        super().__init__(code)
        if kind not in KINDS:raise ValueError('Invalid recovery kind')
        self.recovery_kind=kind;self.fields=list(fields);self.required=list(required);self.tools=list(tools)


def recovery(error=None,*,kind=None,fields=(),required=(),tools=()):
    """Unknown exceptions stop; validators explicitly opt into plan repair."""
    value=kind or getattr(error,'recovery_kind','unavailable')
    if value not in KINDS:value='unavailable'
    return {'kind':value,'invalid_fields':list(fields or getattr(error,'fields',())),
        'required_inputs':list(required or getattr(error,'required',())),
        'allowed_tools':list(tools or getattr(error,'tools',())),
        'provider_retry_allowed':False,'write_authorized':False}
