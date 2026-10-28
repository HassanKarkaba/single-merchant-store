"""Safe authentication and field-validation failures."""
class IdentityError(PermissionError):
    def __init__(self,code,status=401,field_errors=None):
        self.code,self.status=code,status
        self.field_errors=dict(field_errors or {})
        super().__init__(code)
