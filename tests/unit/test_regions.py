import pytest
from pydantic import ValidationError
from app import regions
from app.intake import booking
from app.intake.extract import ExtractedAddress
from app.operations.schemas import Address, DispatchZone


def test_province_and_state_codes_and_names():
    assert regions.province('British Columbia') == 'BC' and regions.province('b.c.') == 'BC'
    assert regions.province('Washington') == 'WA' and regions.province('ny') == 'NY' and regions.province('D.C.') == 'DC'
    assert regions.province('WA', 'CA') is None and regions.province('BC', 'US') is None and regions.province('Bavaria') is None


def test_postal_codes_and_countries():
    assert regions.postal('v6a1a1') == 'V6A 1A1' and regions.postal('98101') == '98101' and regions.postal('981011234') == '98101-1234'
    assert regions.postal('9810') is None and regions.postal('V6A 1A1', 'US') is None
    assert regions.country('BC', 'V6A 1A1') == 'CA' and regions.country('WA', '98101') == 'US' and regions.country('TX') == 'US'
    assert regions.country('BC', '98101') is None


def test_postal_areas_accept_fsa_and_zip_prefixes():
    assert regions.area('v6a 1a1') == 'V6A' and regions.area('981') == '981' and regions.area('98101-1234') == '98101'
    with pytest.raises(ValueError): regions.area('9810')
    assert DispatchZone(name='Seattle', postal_codes=['981', 'V6A 2B2']).postal_codes == ['981', 'V6A']


def test_address_accepts_us_and_keeps_country_consistent():
    us = Address(text='400 Broad St, Seattle, WA 98109', city='Seattle', province='WA', postal_code='98109')
    assert us.country == 'US'
    assert Address(text='1 Main St, Vancouver, BC V6A 1A1', city='Vancouver', province='BC', postal_code='V6A 1A1').country == 'CA'
    with pytest.raises(ValidationError): Address(text='400 Broad St, Seattle', city='Seattle', province='WA', postal_code='98109', country='CA')
    with pytest.raises(ValidationError): Address(text='1 Main St, Vancouver', city='Vancouver', province='BC', postal_code='V6A 1A1', country='US')
    with pytest.raises(ValidationError): Address(text='400 Broad St, Seattle', city='Seattle', province='Washington', postal_code='98109', country='US')


def stop(street, city, province, postal_code):
    return type('Stop', (), {'kind': 'DROPOFF', 'use_shipper_warehouse': False,
        'address': ExtractedAddress(street=street, city=city, province=province, postal_code=postal_code)})()


def test_email_booking_address_reads_us_and_rejects_mixed_countries():
    missing = []
    found = booking.address(stop('400 Broad St', 'Seattle', 'Washington', '98109'), 1, {}, lambda text, postal: None, missing)
    assert found == {'text': '400 Broad St, Seattle, WA 98109, USA', 'city': 'Seattle', 'province': 'WA', 'postal_code': '98109', 'country': 'US'}
    assert missing == []
    booking.address(stop('400 Broad St', 'Seattle', 'BC', '98109'), 1, {}, lambda text, postal: None, missing)
    assert missing and 'incomplete' in missing[0]
