import pytest

from agent import vault


@pytest.mark.parametrize("address,expected", [
    (" Jane.Doe+ADT@GoogleMail.com ", "janedoe+adt@gmail.com"),
    ("Jane.Doe+other@gmail.com", "janedoe+other@gmail.com"),
    ("Jane.Doe+ADT@company.example", "jane.doe+adt@company.example"),
])
def test_canonical_email_keeps_employer_alias(address, expected):
    assert vault.canonical_email(address) == expected


def test_email_search_addresses_expand_only_gmail_spelling():
    addresses = vault.email_search_addresses("jane.doe+adt@gmail.com")
    assert "janedoe+adt@googlemail.com" in addresses
    assert all("+adt@" in address for address in addresses)
    assert vault.email_search_addresses("jane.doe+adt@company.example") == ("jane.doe+adt@company.example",)
