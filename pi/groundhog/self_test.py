"""Non-destructive controller tests. Never calls arm/execute/install/main."""
import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
import xml.etree.ElementTree as ET

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from control import Blocked, check_pins, verify_receipt
from build_image import newc


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.key.public_key().public_numbers()
        def b64(number):
            return base64.b64encode(number.to_bytes((number.bit_length()+7)//8,'big')).decode()
        self.xml = f'<RSAKeyValue><Modulus>{b64(numbers.n)}</Modulus><Exponent>{b64(numbers.e)}</Exponent></RSAKeyValue>'
        self.now = datetime.now(timezone.utc)
        self.config = {'pi_serial':'0123456789abcdef','sd_cid':'a'*32}
        self.data = {'format':'salonmax-pc-backup-receipt','version':1,
                     'created_at':self.now.isoformat(),'cloud_status':'ok',
                     'business_data_sha256':'b'*64, **self.config}

    def sign(self):
        payload=json.dumps(self.data).encode()
        return {'payload':base64.b64encode(payload).decode(),
                'signature':base64.b64encode(self.key.sign(payload,padding.PKCS1v15(),hashes.SHA256())).decode()}

    def test_signature_and_binding(self):
        self.assertEqual(verify_receipt(self.sign(),self.xml,self.config,'b'*64,self.now)['version'],1)
        receipt=self.sign(); receipt['payload']=base64.b64encode(b'{}').decode()
        with self.assertRaises(InvalidSignature): verify_receipt(receipt,self.xml,self.config,'b'*64,self.now)
        with self.assertRaises(Blocked): verify_receipt(self.sign(),self.xml,self.config,'c'*64,self.now)
        with self.assertRaises(Blocked): verify_receipt(self.sign(),self.xml,{'pi_serial':'wrong','sd_cid':'a'*32},'b'*64,self.now)

    def test_expired_future_and_partial_backup(self):
        self.data['created_at']=(self.now-timedelta(hours=25)).isoformat()
        with self.assertRaises(Blocked): verify_receipt(self.sign(),self.xml,self.config,'b'*64,self.now)
        self.data['created_at']=(self.now+timedelta(minutes=1)).isoformat()
        with self.assertRaises(Blocked): verify_receipt(self.sign(),self.xml,self.config,'b'*64,self.now)
        self.data['created_at']=self.now.isoformat();self.data['cloud_status']='failed'
        with self.assertRaises(Blocked): verify_receipt(self.sign(),self.xml,self.config,'b'*64,self.now)

    def test_two_pin_and_role_checks(self):
        db=sqlite3.connect(':memory:')
        try:
            db.executescript("create table staff_users(id integer,pin_code text,role text,is_active integer);"
                             "insert into staff_users values(1,'1111','owner',1),(2,'2222','manager',1),(3,'3333','staff',1),(4,'4444','manager',0);")
            request={'owner_id':1,'owner_pin':'1111','manager_pin':'2222','confirmation':'ERASE SD CARD'}
            check_pins(db,request)
            for key,value in [('owner_id',3),('owner_pin','wrong'),('manager_pin','3333'),('manager_pin','4444'),('confirmation','yes')]:
                with self.assertRaises(Blocked): check_pins(db,{**request,key:value})
        finally: db.close()

    def test_ram_archive_format(self):
        archive=newc([('init',0o100755,b'#!/bin/busybox sh\n',0,0)])
        self.assertTrue(archive.startswith(b'070701'))
        self.assertIn(b'TRAILER!!!\0',archive)
        self.assertEqual(len(archive)%4,0)


if __name__=='__main__':
    unittest.main()
