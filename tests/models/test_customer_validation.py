"""Pure customer-input boundary checks, without database or network access."""
import pytest
from app.models.identity.accounts import canonical_email,public_password,token_digest
from app.models.identity import IdentityError


def test_email_canonicalization_and_full254_boundary():
    assert canonical_email(' NAME+Tag@BÜCHER.Example ')=='name+tag@xn--bcher-kva.example'
    maximum='a'*64+'@'+'b'*63+'.'+'c'*63+'.'+'d'*61
    assert len(maximum)==254 and canonical_email(maximum)==maximum
    with pytest.raises(IdentityError):canonical_email(maximum+'x')


@pytest.mark.parametrize('value',[None,True,'plain-name','a@@example.com','a b@example.com','a\r\n@example.com','.a@example.com','a..b@example.com','a.@example.com','a@localhost','a@-bad.example','a'*65+'@example.com'])
def test_invalid_email_has_safe_field_error(value):
    with pytest.raises(IdentityError) as error:canonical_email(value)
    assert error.value.code=='invalid_email' and error.value.status==422
    assert error.value.field_errors=={'email':'Enter a valid email address.'}


def test_password_unicode_length_confirmation_and_safe_failure():
    assert public_password('x'*12,'x'*12)=='x'*12
    assert public_password('🔒'*128,'🔒'*128)=='🔒'*128
    for password in ('short','x'*129,'x'*12+'\x00','\ud800'*12):
        with pytest.raises(IdentityError) as error:public_password(password,password)
        assert error.value.code=='password_policy' and 'password' in error.value.field_errors
    with pytest.raises(IdentityError) as error:public_password('A correct password','Something different')
    assert error.value.code=='password_confirmation_mismatch'
    assert 'Something different' not in str(error.value.field_errors)


def test_purpose_bound_token_digest_and_invalid_wire_format():
    token='a'*43
    first=token_digest('email_verification',token);second=token_digest('password_reset',token)
    assert len(first)==len(second)==64 and first!=second and token not in first
    for bad in (None,'',token+'a','a'*42+'!'):
        with pytest.raises(IdentityError):token_digest('email_verification',bad)
